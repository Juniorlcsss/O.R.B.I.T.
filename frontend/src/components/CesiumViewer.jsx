import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as Cesium from "cesium";
import "cesium/Build/Cesium/Widgets/widgets.css";
import { IconCrosshair, IconLock, IconReset } from "./icons.jsx";
import { isOurAsset, num, objectLabel, objectRole } from "../lib/format.js";
import useSettings from "../hooks/useSettings.jsx";

/*
 * The globe runs on a purpose-built orbit camera rather than Cesium's default
 * free-flight controller.
 *
 * The default rig lets an operator roll the horizon, fly off into empty space
 * and lose the Earth entirely — during a live demo that is a disaster. Instead
 * the camera is fully described by three numbers (longitude, latitude,
 * altitude) and always points at the Earth's centre, so the globe is pinned to
 * the middle of frame no matter where you drag. The up-vector is recomputed
 * towards the north pole every frame, which makes roll mathematically
 * impossible. Drag horizontally to spin, vertically to climb towards a pole.
 * `L` freezes the vertical axis for a locked-off shot.
 *
 * Everything colour-coded here — object class, risk band — is driven by the
 * accessibility palette, and can additionally carry a line pattern so the risk
 * bands stay separable without relying on hue.
 */


// Camera envelope. Altitudes are metres above the WGS84 ellipsoid.
const MIN_ALT = 1_400_000;
const MAX_ALT = 58_000_000;
const HOME_ALT = 21_000_000;
const HOME_LON = -32;
const HOME_LAT = 12; // Opening tilt: enough to read as 3D, still equatorial.
const FREE_LAT_LIMIT = 78; // Never reach the pole, where the up-vector degenerates.

// Feel. All per-frame at 60 Hz.
const SMOOTHING = 0.16;
const INERTIA_DECAY = 0.9;
const IDLE_SPIN_DEG = 0.022;
const IDLE_AFTER_MS = 7_000;
const KEY_PAN_DEG = 2.6;
const KEY_TILT_DEG = 2.2;
const KEY_ZOOM_FACTOR = 1.12;

// Maneuver animation.
const MANEUVER_DURATION_MS = 5_000;
const MANEUVER_LON_OFFSET_DEG = 0.55;
const MANEUVER_LABEL_LINGER_MS = 4_000;
const CONJUNCTION_LINK_MAX_M = 4_000_000;

const TRAIL_LENGTH = 110;
const TRAIL_ALPHA = 0.3;

const SELECT_OUTLINE = Cesium.Color.WHITE;
const LABEL_BG = Cesium.Color.fromCssColorString("#06080b").withAlpha(0.78);

/** Dash patterns give each risk band a texture, not just a hue. */
const DASH_PATTERNS = { LOW: 0b1111111111111111, MEDIUM: 0b1111000011110000, HIGH: 0b1100110011001100 };

const clamp = (value, low, high) => Math.min(Math.max(value, low), high);
const easeInOutCubic = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
const lerp = (a, b, t) => a + (b - a) * t;
/** Signed shortest angular distance, so a track never unwinds the long way. */
const shortestLonDelta = (from, to) => ((((to - from) % 360) + 540) % 360) - 180;
const wrapLon = (lon) => ((((lon + 180) % 360) + 360) % 360) - 180;

/** Assign an optional Cesium property without exploding on older builds. */
function tune(target, key, value) {
  if (target && key in target) target[key] = value;
}

export default function CesiumViewer({ objects, conjunctions, maneuver, selectedId, onSelect }) {
  const { palette, risk, settings, reducedMotion, shapes } = useSettings();

  const containerRef = useRef(null);
  const viewerRef = useRef(null);

  // Interpolation between the last two /api/orbital_state snapshots.
  const samplesRef = useRef(new Map());
  const trailsRef = useRef(new Map());
  const snapshotRef = useRef({});

  const entitiesRef = useRef(new Map());
  const linesRef = useRef(new Map());
  const trailEntitiesRef = useRef(new Map());
  const offsetsRef = useRef(new Map());
  const maneuverRef = useRef(null);
  const selectedRef = useRef(null);
  const selectRef = useRef(onSelect);
  const motionRef = useRef(reducedMotion);

  // The whole camera state, mutated in place so the render loop never allocates.
  const camRef = useRef({
    lon: HOME_LON,
    lat: HOME_LAT,
    alt: MAX_ALT,
    tLon: HOME_LON,
    tLat: HOME_LAT,
    tAlt: HOME_ALT,
    vLon: 0,
    dragging: false,
    lastInputMs: Date.now(),
    locked: false,
    followId: null,
  });

  // Entities are cheap to rebuild and styling changes are rare, so a signature
  // is tracked and the scene rebuilt, rather than threading every colour
  // through a per-frame callback.
  const styleKey = `${settings.cvd}|${shapes}|${settings.labels}|${settings.scale}|${settings.typeface}`;
  const styleRef = useRef(styleKey);
  const lineStyleRef = useRef(styleKey);

  const colors = useMemo(
    () => ({
      satellite: Cesium.Color.fromCssColorString(palette.satellite),
      satelliteOutline: Cesium.Color.fromCssColorString(palette.satellite).darken(0.75, new Cesium.Color()),
      satelliteLabel: Cesium.Color.fromCssColorString(palette.satellite).brighten(0.55, new Cesium.Color()),
      debris: Cesium.Color.fromCssColorString(palette.debris),
      debrisOutline: Cesium.Color.fromCssColorString(palette.debris).darken(0.75, new Cesium.Color()),
      debrisLabel: Cesium.Color.fromCssColorString(palette.debris).brighten(0.5, new Cesium.Color()),
      caution: Cesium.Color.fromCssColorString(palette.caution),
      exercise: Cesium.Color.fromCssColorString(palette.caution),
      exerciseOutline: Cesium.Color.fromCssColorString(palette.caution).darken(0.7, new Cesium.Color()),
      exerciseLabel: Cesium.Color.fromCssColorString(palette.caution).brighten(0.5, new Cesium.Color()),
      nominal: Cesium.Color.fromCssColorString(palette.nominal),
      band: {
        LOW: Cesium.Color.fromCssColorString(risk.LOW),
        MEDIUM: Cesium.Color.fromCssColorString(risk.MEDIUM),
        HIGH: Cesium.Color.fromCssColorString(risk.HIGH),
      },
    }),
    [palette, risk]
  );

  const labelFont = useMemo(() => {
    const family = settings.typeface === "hyperlegible" ? "'Atkinson Hyperlegible'" : "'IBM Plex Mono'";
    return {
      object: `500 ${Math.round(11 * settings.scale)}px ${family}, monospace`,
      maneuver: `700 ${Math.round(12 * settings.scale)}px ${family}, monospace`,
    };
  }, [settings.typeface, settings.scale]);

  const [locked, setLocked] = useState(false);
  const [following, setFollowing] = useState(null);
  const [hint, setHint] = useState(true);
  const [viewerReady, setViewerReady] = useState(false);

  useEffect(() => {
    selectRef.current = onSelect;
  }, [onSelect]);

  useEffect(() => {
    motionRef.current = reducedMotion;
  }, [reducedMotion]);

  useEffect(() => {
    camRef.current.locked = locked;
    if (locked) camRef.current.tLat = HOME_LAT;
  }, [locked]);

  useEffect(() => {
    selectedRef.current = selectedId ?? null;
  }, [selectedId]);

  useEffect(() => {
    maneuverRef.current = maneuver ? { ...maneuver, folded: false } : null;
  }, [maneuver]);

  // ---------------------------------------------------------------- positions

  const currentGeodetic = useCallback((id) => {
    const pair = samplesRef.current.get(id);
    if (!pair) return null;
    const span = pair.b.t - pair.a.t;
    const p = span > 0 ? clamp((Date.now() - pair.a.t) / span, 0, 1) : 1;
    return {
      lat: lerp(pair.a.lat, pair.b.lat, p),
      lon: pair.a.lon + shortestLonDelta(pair.a.lon, pair.b.lon) * p,
      alt: lerp(pair.a.alt, pair.b.alt, p),
    };
  }, []);

  /** Extra longitude a satellite has picked up from an executing maneuver. */
  const maneuverLonOffset = useCallback((id) => {
    const state = maneuverRef.current;
    const settled = offsetsRef.current.get(id) || 0;
    if (!state || state.satId !== id) return settled;
    const elapsed = Date.now() - state.startedAt;
    if (elapsed <= 0) return settled;
    const progress = Math.min(elapsed / MANEUVER_DURATION_MS, 1);
    if (progress >= 1 && !state.folded) {
      offsetsRef.current.set(id, settled + MANEUVER_LON_OFFSET_DEG);
      state.folded = true;
    }
    return settled + MANEUVER_LON_OFFSET_DEG * easeInOutCubic(progress);
  }, []);

  const currentCartesian = useCallback(
    (id) => {
      const geo = currentGeodetic(id);
      if (!geo) return undefined;
      return Cesium.Cartesian3.fromDegrees(geo.lon + maneuverLonOffset(id), geo.lat, geo.alt * 1000);
    },
    [currentGeodetic, maneuverLonOffset]
  );

  const maneuverPhase = useCallback(() => {
    const state = maneuverRef.current;
    if (!state) return "idle";
    const elapsed = Date.now() - state.startedAt;
    if (elapsed < 0) return "idle";
    if (elapsed <= MANEUVER_DURATION_MS) return "executing";
    if (elapsed <= MANEUVER_DURATION_MS + MANEUVER_LABEL_LINGER_MS) return "done";
    return "idle";
  }, []);

  // Ingest each snapshot: shift the interpolation window and extend the trails.
  useEffect(() => {
    const latest = {};
    const nowMs = Date.now();

    (objects || []).forEach((obj) => {
      latest[obj.id] = obj;
      const sample = { lat: obj.lat, lon: obj.lon, alt: obj.alt_km, t: nowMs };
      const previous = samplesRef.current.get(obj.id);
      samplesRef.current.set(
        obj.id,
        previous ? { a: previous.b, b: sample } : { a: sample, b: { ...sample, t: nowMs + 1_000 } }
      );

      const trail = trailsRef.current.get(obj.id) || [];
      trail.push(Cesium.Cartesian3.fromDegrees(obj.lon, obj.lat, obj.alt_km * 1000));
      if (trail.length > TRAIL_LENGTH) trail.shift();
      trailsRef.current.set(obj.id, trail);
    });

    snapshotRef.current = latest;
  }, [objects]);

  // ------------------------------------------------------------------ camera

  const applyCamera = useCallback(() => {
    const viewer = viewerRef.current;
    if (!viewer) return;
    const cam = camRef.current;

    const position = Cesium.Cartesian3.fromDegrees(cam.lon, cam.lat, cam.alt);
    // Look straight at the Earth's centre...
    const direction = Cesium.Cartesian3.normalize(
      Cesium.Cartesian3.negate(position, new Cesium.Cartesian3()),
      new Cesium.Cartesian3()
    );
    // ...with "right" pinned due east, which forces roll to zero and keeps
    // north up no matter how far the operator has spun the globe.
    const east = Cesium.Cartesian3.normalize(
      Cesium.Cartesian3.cross(Cesium.Cartesian3.UNIT_Z, position, new Cesium.Cartesian3()),
      new Cesium.Cartesian3()
    );
    const up = Cesium.Cartesian3.normalize(
      Cesium.Cartesian3.cross(east, direction, new Cesium.Cartesian3()),
      new Cesium.Cartesian3()
    );
    viewer.camera.setView({ destination: position, orientation: { direction, up } });
  }, []);

  const noteInput = useCallback(() => {
    camRef.current.lastInputMs = Date.now();
  }, []);

  const zoomBy = useCallback(
    (factor) => {
      camRef.current.tAlt = clamp(camRef.current.tAlt * factor, MIN_ALT, MAX_ALT);
      noteInput();
    },
    [noteInput]
  );

  const releaseFollow = useCallback(() => {
    camRef.current.followId = null;
    setFollowing(null);
  }, []);

  const resetView = useCallback(() => {
    const cam = camRef.current;
    cam.followId = null;
    cam.vLon = 0;
    cam.tLon = HOME_LON;
    cam.tLat = HOME_LAT;
    cam.tAlt = HOME_ALT;
    setFollowing(null);
    noteInput();
  }, [noteInput]);

  const followObject = useCallback(
    (id) => {
      if (!id) return;
      camRef.current.followId = id;
      camRef.current.vLon = 0;
      camRef.current.tAlt = clamp(camRef.current.tAlt, MIN_ALT, 9_000_000);
      setFollowing(id);
      noteInput();
    },
    [noteInput]
  );

  // ------------------------------------------------------------ viewer setup

  useEffect(() => {
    if (!containerRef.current || viewerRef.current) return;

    const viewer = new Cesium.Viewer(containerRef.current, {
      baseLayer: Cesium.ImageryLayer.fromProviderAsync(
        Cesium.TileMapServiceImageryProvider.fromUrl(Cesium.buildModuleUrl("Assets/Textures/NaturalEarthII"))
      ),
      baseLayerPicker: false,
      geocoder: false,
      homeButton: false,
      sceneModePicker: false,
      navigationHelpButton: false,
      animation: false,
      timeline: false,
      fullscreenButton: false,
      infoBox: false,
      selectionIndicator: false,
      shouldAnimate: true,
    });
    viewerRef.current = viewer;
    // Dev-only handle for poking at the scene from the console. Stripped from
    // production builds by the import.meta.env.DEV guard.
    if (import.meta.env.DEV) window.__orbitViewer = viewer;

    const scene = viewer.scene;
    const globe = scene.globe;

    globe.enableLighting = true;
    globe.showGroundAtmosphere = true;
    globe.baseColor = Cesium.Color.fromCssColorString("#071522");
    tune(globe, "dynamicAtmosphereLighting", true);
    tune(globe, "atmosphereLightIntensity", 12.0);
    tune(globe, "atmosphereBrightnessShift", 0.08);

    tune(scene.skyAtmosphere, "atmosphereLightIntensity", 22.0);
    tune(scene.skyAtmosphere, "saturationShift", -0.08);
    tune(scene.skyAtmosphere, "brightnessShift", -0.05);

    scene.fog.enabled = false;
    scene.postProcessStages.fxaa.enabled = true;
    // Asking for HDR on a context that cannot do it is a hard error in Cesium.
    if (scene.highDynamicRangeSupported) scene.highDynamicRange = true;

    // Just enough bloom to make points read as light sources, not stickers.
    const bloom = scene.postProcessStages.bloom;
    if (bloom) {
      bloom.enabled = true;
      tune(bloom.uniforms, "glowOnly", false);
      tune(bloom.uniforms, "contrast", 128);
      tune(bloom.uniforms, "brightness", -0.45);
      tune(bloom.uniforms, "delta", 1.0);
      tune(bloom.uniforms, "sigma", 2.0);
      tune(bloom.uniforms, "stepSize", 1.0);
    }

    // Hand every input to our own controller.
    const ssc = scene.screenSpaceCameraController;
    ssc.enableRotate = false;
    ssc.enableTranslate = false;
    ssc.enableZoom = false;
    ssc.enableTilt = false;
    ssc.enableLook = false;
    ssc.enableCollisionDetection = false;

    applyCamera();
    setViewerReady(true);

    const onTick = () => {
      const cam = camRef.current;
      const still = motionRef.current;

      if (cam.followId) {
        const geo = currentGeodetic(cam.followId);
        if (geo) {
          cam.tLon = geo.lon + maneuverLonOffset(cam.followId);
          if (!cam.locked) cam.tLat = clamp(geo.lat, -FREE_LAT_LIMIT, FREE_LAT_LIMIT);
        }
      } else if (!cam.dragging) {
        if (!still && Math.abs(cam.vLon) > 1e-3) {
          cam.tLon = wrapLon(cam.tLon + cam.vLon);
          cam.vLon *= INERTIA_DECAY;
        } else {
          cam.vLon = 0;
          // Idle drift keeps the scene alive without hijacking control, but it
          // is exactly the unrequested movement reduced-motion exists to stop.
          if (!still && Date.now() - cam.lastInputMs > IDLE_AFTER_MS) {
            cam.tLon = wrapLon(cam.tLon + IDLE_SPIN_DEG);
          }
        }
      }

      cam.tLat = cam.locked ? HOME_LAT : clamp(cam.tLat, -FREE_LAT_LIMIT, FREE_LAT_LIMIT);

      const ease = still ? 1 : SMOOTHING;
      cam.lon = wrapLon(cam.lon + shortestLonDelta(cam.lon, cam.tLon) * ease);
      cam.lat += (cam.tLat - cam.lat) * ease;
      cam.alt += (cam.tAlt - cam.alt) * (still ? 1 : SMOOTHING * 0.8);

      applyCamera();
    };
    viewer.clock.onTick.addEventListener(onTick);

    // ---- pointer, wheel and keyboard control -------------------------------

    const canvas = scene.canvas;
    let activePointer = null;
    let lastX = 0;
    let lastY = 0;

    const degreesPerPixel = () => clamp(0.055 * (camRef.current.tAlt / 1e7), 0.012, 0.22);

    const onPointerDown = (event) => {
      if (event.button !== 0) return;
      activePointer = event.pointerId;
      lastX = event.clientX;
      lastY = event.clientY;
      camRef.current.dragging = true;
      camRef.current.vLon = 0;
      noteInput();
      // Throws NotFoundError if the pointer was released between the event
      // being queued and handled; losing capture is survivable, dropping the
      // drag is not.
      try {
        canvas.setPointerCapture?.(event.pointerId);
      } catch {
        /* continue without capture */
      }
      canvas.style.cursor = "grabbing";
    };

    const onPointerMove = (event) => {
      if (activePointer !== event.pointerId) return;
      const dx = event.clientX - lastX;
      const dy = event.clientY - lastY;
      lastX = event.clientX;
      lastY = event.clientY;

      const cam = camRef.current;
      const scale = degreesPerPixel();
      cam.followId = null;
      cam.tLon = wrapLon(cam.tLon - dx * scale);
      // Blend the drag into the fling velocity so release feels continuous.
      cam.vLon = cam.vLon * 0.6 - dx * scale * 0.4;
      if (!cam.locked) cam.tLat = clamp(cam.tLat + dy * scale, -FREE_LAT_LIMIT, FREE_LAT_LIMIT);
      noteInput();
    };

    const endDrag = (event) => {
      if (activePointer === null) return;
      if (event && event.pointerId !== undefined && event.pointerId !== activePointer) return;
      activePointer = null;
      camRef.current.dragging = false;
      noteInput();
      canvas.style.cursor = "grab";
    };

    const onWheel = (event) => {
      event.preventDefault();
      const steps = clamp(event.deltaY, -240, 240) / 240;
      camRef.current.tAlt = clamp(camRef.current.tAlt * Math.exp(steps * 0.55), MIN_ALT, MAX_ALT);
      noteInput();
    };

    canvas.style.cursor = "grab";
    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);
    canvas.addEventListener("wheel", onWheel, { passive: false });

    // Selection and follow, via Cesium picking so we hit the real entities.
    const handler = new Cesium.ScreenSpaceEventHandler(canvas);
    const pickId = (position) => {
      const picked = scene.pick(position);
      const id = picked?.id?.id;
      return typeof id === "string" && snapshotRef.current[id] ? id : null;
    };
    handler.setInputAction((movement) => {
      const id = pickId(movement.position);
      selectRef.current?.(id);
      if (!id) releaseFollow();
    }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
    handler.setInputAction((movement) => {
      const id = pickId(movement.position);
      if (!id) return;
      selectRef.current?.(id);
      followObject(id);
    }, Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);

    return () => {
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", endDrag);
      canvas.removeEventListener("pointercancel", endDrag);
      canvas.removeEventListener("wheel", onWheel);
      handler.destroy();
      viewer.clock.onTick.removeEventListener(onTick);
      viewer.destroy();
      viewerRef.current = null;
      entitiesRef.current.clear();
      linesRef.current.clear();
      trailEntitiesRef.current.clear();
    };
  }, [applyCamera, currentGeodetic, followObject, maneuverLonOffset, noteInput, releaseFollow]);

  // Keyboard control lives on the window so the globe never has to hold focus.
  useEffect(() => {
    const onKeyDown = (event) => {
      const target = event.target;
      if (target instanceof HTMLElement) {
        if (/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
        // An open dialog owns the keyboard.
        if (target.closest('[role="dialog"]')) return;
      }
      const cam = camRef.current;
      switch (event.key) {
        case "ArrowLeft":
          cam.followId = null;
          setFollowing(null);
          cam.tLon = wrapLon(cam.tLon - KEY_PAN_DEG);
          break;
        case "ArrowRight":
          cam.followId = null;
          setFollowing(null);
          cam.tLon = wrapLon(cam.tLon + KEY_PAN_DEG);
          break;
        case "ArrowUp":
          if (cam.locked) return;
          cam.tLat = clamp(cam.tLat + KEY_TILT_DEG, -FREE_LAT_LIMIT, FREE_LAT_LIMIT);
          break;
        case "ArrowDown":
          if (cam.locked) return;
          cam.tLat = clamp(cam.tLat - KEY_TILT_DEG, -FREE_LAT_LIMIT, FREE_LAT_LIMIT);
          break;
        case "+":
        case "=":
          zoomBy(1 / KEY_ZOOM_FACTOR);
          break;
        case "-":
        case "_":
          zoomBy(KEY_ZOOM_FACTOR);
          break;
        case "l":
        case "L":
          setLocked((value) => !value);
          break;
        case "r":
        case "R":
          resetView();
          break;
        case "Escape":
          releaseFollow();
          break;
        default:
          return;
      }
      event.preventDefault();
      noteInput();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [noteInput, releaseFollow, resetView, zoomBy]);

  const acquiredRef = useRef(false);
  useEffect(() => {
    if (acquiredRef.current) return;
    const target = (objects || []).find((o) => o.owned) || (objects || [])[0];
    if (!target) return;
    acquiredRef.current = true;
    camRef.current.tLon = target.lon;
    camRef.current.tAlt = 15_000_000;
  }, [objects]);

  // ---------------------------------------------------------------- entities

  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer) return;

    // A styling change makes every cached entity stale; drop and rebuild.
    if (styleRef.current !== styleKey) {
      styleRef.current = styleKey;
      entitiesRef.current.forEach((entity) => viewer.entities.remove(entity));
      trailEntitiesRef.current.forEach((entity) => viewer.entities.remove(entity));
      entitiesRef.current.clear();
      trailEntitiesRef.current.clear();
    }

    const showAllLabels = settings.labels === "all";
    const sizeScale = settings.scale;

    const seen = new Set();
    (objects || []).forEach((obj) => {
      seen.add(obj.id);
      if (entitiesRef.current.has(obj.id)) return;

      const isSatellite = obj.type === "satellite";
      const isExercise = obj.exercise === true;
      const baseName = obj.name.replace(/\s*\(.*?\)\s*/g, "").trim();
      const shortName = isExercise ? `SIM ${baseName}` : baseName;

      const entity = viewer.entities.add({
        id: obj.id,
        name: shortName,
        position: new Cesium.CallbackProperty(() => currentCartesian(obj.id), false),
        point: {
          pixelSize: new Cesium.CallbackProperty(() => {
            const base = (isSatellite ? 9 : 5.5) * sizeScale;
            // Pulsing is the seizure-risk part of the maneuver callout, so it
            // is the part reduced motion drops.
            if (!motionRef.current && maneuverPhase() === "executing" && maneuverRef.current?.satId === obj.id) {
              return base + 6 * Math.abs(Math.sin((Date.now() - maneuverRef.current.startedAt) / 150));
            }
            return selectedRef.current === obj.id ? base + 3 : base;
          }, false),
          color: isExercise ? colors.exercise : isSatellite ? colors.satellite : colors.debris,
          outlineColor: new Cesium.CallbackProperty(
            () =>
              selectedRef.current === obj.id
                ? SELECT_OUTLINE
                : isExercise
                  ? colors.exerciseOutline
                  : isSatellite
                    ? colors.satelliteOutline
                    : colors.debrisOutline,
            false
          ),
          outlineWidth: new Cesium.CallbackProperty(() => (selectedRef.current === obj.id ? 2.5 : 1.5), false),
        },
        label: {
          text: shortName,
          font: labelFont.object,
          fillColor: isExercise ? colors.exerciseLabel : isSatellite ? colors.satelliteLabel : colors.debrisLabel,
          style: Cesium.LabelStyle.FILL,
          pixelOffset: new Cesium.Cartesian2(10, -10),
          horizontalOrigin: Cesium.HorizontalOrigin.LEFT,
          showBackground: true,
          backgroundColor: LABEL_BG,
          backgroundPadding: new Cesium.Cartesian2(5, 3),
          // Debris is only named when it matters. Labelling all of it turns the
          // globe into a wall of text, which is what a real console avoids —
          // but an operator can force every label on from settings.
          show: new Cesium.CallbackProperty(
            () =>
              showAllLabels ||
              isExercise ||
              isSatellite ||
              selectedRef.current === obj.id ||
              camRef.current.alt < 11_000_000,
            false
          ),
        },
      });
      entitiesRef.current.set(obj.id, entity);

      // Ground-track history. Thin and low-contrast: context, not content.
      const trailColor = isExercise ? colors.exercise : isSatellite ? colors.satellite : colors.debris;
      const trail = viewer.entities.add({
        id: `trail-${obj.id}`,
        polyline: {
          // Copied, not aliased: the buffer is mutated in place every snapshot.
          positions: new Cesium.CallbackProperty(() => (trailsRef.current.get(obj.id) || []).slice(), false),
          width: (isSatellite ? 1.4 : 1) * sizeScale,
          arcType: Cesium.ArcType.NONE,
          material: trailColor.withAlpha(TRAIL_ALPHA),
          depthFailMaterial: trailColor.withAlpha(TRAIL_ALPHA * 0.35),
        },
      });
      trailEntitiesRef.current.set(obj.id, trail);
    });

    entitiesRef.current.forEach((entity, id) => {
      if (seen.has(id)) return;
      viewer.entities.remove(entity);
      entitiesRef.current.delete(id);
      const trail = trailEntitiesRef.current.get(id);
      if (trail) viewer.entities.remove(trail);
      trailEntitiesRef.current.delete(id);
      samplesRef.current.delete(id);
      trailsRef.current.delete(id);
      offsetsRef.current.delete(id);
    });
  }, [objects, currentCartesian, maneuverPhase, colors, labelFont, settings.labels, settings.scale, styleKey]);

  // Conjunction lines: the geometry of the risk, drawn only while it is live.
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer) return;

    // Colour or pattern changed: rebuild every line rather than mutate.
    if (lineStyleRef.current !== styleKey) {
      lineStyleRef.current = styleKey;
      linesRef.current.forEach((entity) => viewer.entities.remove(entity));
      linesRef.current.clear();
    }

    const wanted = new Set((conjunctions || []).map((c) => `${c.sat_id}->${c.debris_id}`));
    linesRef.current.forEach((entity, key) => {
      if (wanted.has(key)) return;
      viewer.entities.remove(entity);
      linesRef.current.delete(key);
    });

    (conjunctions || []).forEach((conj) => {
      const key = `${conj.sat_id}->${conj.debris_id}`;
      if (linesRef.current.has(key)) return;
      const band = conj.risk_band in colors.band ? conj.risk_band : "LOW";
      const color = colors.band[band];
      const closeness = 1 - Math.min(conj.miss_distance_km / 25, 1);
      // With shape coding on, each band also gets its own dash pattern, so the
      // three risk levels stay distinguishable with no hue at all.
      const material = shapes
        ? new Cesium.PolylineDashMaterialProperty({
            color: color.withAlpha(0.95),
            dashLength: 22,
            dashPattern: DASH_PATTERNS[band],
          })
        : new Cesium.PolylineGlowMaterialProperty({
            glowPower: 0.18,
            taperPower: 0.85,
            color: color.withAlpha(0.9),
          });

      const entity = viewer.entities.add({
        id: `conjunction-${key}`,
        polyline: {
          positions: new Cesium.CallbackProperty(() => {
            const from = currentCartesian(conj.sat_id);
            const to = currentCartesian(conj.debris_id);
            if (!from || !to) return [];
            return Cesium.Cartesian3.distance(from, to) > CONJUNCTION_LINK_MAX_M
              ? []
              : [from, to];
          }, false),
          width: (1.5 + 3.5 * closeness) * settings.scale,
          arcType: Cesium.ArcType.NONE,
          material,
        },
      });
      linesRef.current.set(key, entity);
    });
  }, [conjunctions, currentCartesian, colors, shapes, settings.scale, styleKey]);

  // Maneuver callout, anchored above the burning satellite.
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer || !maneuver) return;

    const labelId = `maneuver-${maneuver.satId}`;
    if (viewer.entities.getById(labelId)) return;

    viewer.entities.add({
      id: labelId,
      position: new Cesium.CallbackProperty(() => {
        const cartesian = currentCartesian(maneuverRef.current?.satId);
        if (!cartesian) return undefined;
        const carto = Cesium.Cartographic.fromCartesian(cartesian);
        return Cesium.Cartesian3.fromRadians(carto.longitude, carto.latitude, carto.height + 220_000);
      }, false),
      label: {
        text: new Cesium.CallbackProperty(
          () => (maneuverPhase() === "done" ? "ORBIT UPDATED" : "MANEUVER EXECUTING"),
          false
        ),
        font: labelFont.maneuver,
        fillColor: new Cesium.CallbackProperty(
          () => (maneuverPhase() === "done" ? colors.nominal : colors.caution),
          false
        ),
        style: Cesium.LabelStyle.FILL,
        pixelOffset: new Cesium.Cartesian2(0, -26),
        showBackground: true,
        backgroundColor: LABEL_BG,
        backgroundPadding: new Cesium.Cartesian2(7, 4),
        horizontalOrigin: Cesium.HorizontalOrigin.CENTER,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
      },
    });

    const timer = setInterval(() => {
      if (maneuverPhase() !== "idle") return;
      const label = viewer.entities.getById(labelId);
      if (label) viewer.entities.remove(label);
      clearInterval(timer);
    }, 500);
    return () => clearInterval(timer);
  }, [maneuver, currentCartesian, maneuverPhase, colors, labelFont]);

  // ------------------------------------------------------------------ chrome

  const counts = useMemo(() => {
    const list = objects || [];
    const live = list.filter((o) => !o.exercise);
    return {
      satellites: live.filter((o) => o.type === "satellite").length,
      debris: live.filter((o) => o.type === "debris").length,
      exercise: list.length - live.length,
    };
  }, [objects]);

  const worstBand = useMemo(() => {
    const bands = new Set((conjunctions || []).map((c) => c.risk_band));
    if (bands.has("HIGH")) return "HIGH";
    if (bands.has("MEDIUM")) return "MEDIUM";
    return bands.size ? "LOW" : null;
  }, [conjunctions]);

  const oursScreened = useMemo(() => {
    const ours = new Set((objects || []).filter(isOurAsset).map((o) => o.id));
    return (conjunctions || []).filter((c) => ours.has(c.sat_id) || ours.has(c.debris_id)).length;
  }, [objects, conjunctions]);

  const selected = selectedId ? (objects || []).find((o) => o.id === selectedId) : null;

  return (
    <div className="relative h-full w-full overflow-hidden bg-ink-900">
      <div ref={containerRef} className="h-full w-full" />

      {!viewerReady && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-ink-900">
          <span className="block h-px w-40 overflow-hidden bg-hair">
            <span className="animate-sweep block h-px w-1/4 bg-accent" />
          </span>
          <p className="font-mono text-2xs uppercase tracking-[0.3em] text-fg-3">
            Initialising orbital view
          </p>
        </div>
      )}

      {/* Catalogue summary */}
      <div className="pointer-events-none absolute left-3 top-3 rounded border border-hair bg-ink-900/70 px-3 py-2 backdrop-blur-sm">
        <p className="eyebrow">Tracked catalogue</p>
        <div className="mt-1.5 space-y-1 font-mono text-xs text-fg-2">
          <p className="flex items-center gap-2">
            <span className="h-1.5 w-1.5 rounded-full" style={{ background: palette.satellite }} />
            {counts.satellites} payloads
          </p>
          <p className="flex items-center gap-2">
            <span className="h-1.5 w-1.5 rounded-full" style={{ background: palette.debris }} />
            {counts.debris} debris
          </p>
          {counts.exercise > 0 && (
            <p className="flex items-center gap-2 text-caution">
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: palette.caution }} />
              {counts.exercise} simulated (exercise)
            </p>
          )}
          <p className="pt-0.5 text-fg-3">
            {worstBand ? `${(conjunctions || []).length} screens · worst ${worstBand}` : "no active screens"}
          </p>
          {worstBand && (
            <p className={oursScreened > 0 ? "text-caution" : "text-fg-3"}>
              {oursScreened > 0 ? `${oursScreened} involving our asset` : "none involving our asset"}
            </p>
          )}
        </div>
      </div>

      {/* Camera controls */}
      <div className="absolute bottom-3 left-3 flex flex-col items-start gap-2">
        {hint && (
          <div className="rounded border border-hair bg-ink-900/70 px-3 py-2 font-mono text-2xs leading-5 tracking-normal text-fg-3 backdrop-blur-sm">
            <div className="flex items-baseline justify-between gap-6">
              <span className="eyebrow">Camera</span>
              <button onClick={() => setHint(false)} className="text-fg-3 transition-colors hover:text-fg">
                hide
              </button>
            </div>
            <p className="mt-1.5">
              <span className="text-fg-2">drag</span> / <span className="text-fg-2">arrows</span> orbit &middot;{" "}
              <span className="text-fg-2">wheel</span> / <span className="text-fg-2">+ &minus;</span> zoom
            </p>
            <p>
              <span className="text-fg-2">dbl-click</span> track object &middot; <span className="text-fg-2">esc</span>{" "}
              release
            </p>
            <p>
              <span className="text-fg-2">L</span> {locked ? "free the" : "freeze"} vertical axis &middot;{" "}
              <span className="text-fg-2">R</span> reset view
            </p>
          </div>
        )}

        <div className="flex items-center gap-1 rounded border border-hair bg-ink-900/70 p-1 backdrop-blur-sm">
          <button
            onClick={() => setLocked((value) => !value)}
            aria-pressed={locked}
            title={
              locked
                ? "Vertical axis frozen — horizontal orbit only (L)"
                : "Vertical axis free — drag up/down to climb towards a pole (L)"
            }
            className={`flex items-center gap-1.5 rounded px-2 py-1 font-mono text-2xs tracking-normal transition-colors duration-150 ease-console ${
              locked ? "bg-accent-soft text-accent" : "text-fg-2 hover:text-fg"
            }`}
          >
            <IconLock open={!locked} size={12} />
            {locked ? "y-axis locked" : "y-axis free"}
          </button>
          <span className="h-4 w-px bg-hair" />
          <button
            onClick={resetView}
            aria-label="Reset the view"
            title="Reset view (R)"
            className="rounded px-1.5 py-1 text-fg-2 transition-colors duration-150 ease-console hover:text-fg"
          >
            <IconReset size={12} />
          </button>
          {following && (
            <>
              <span className="h-4 w-px bg-hair" />
              <button
                onClick={releaseFollow}
                title="Stop tracking (Esc)"
                className="flex items-center gap-1.5 rounded px-2 py-1 font-mono text-2xs tracking-normal text-accent"
              >
                <IconCrosshair size={12} />
                {following}
              </button>
            </>
          )}
          {!hint && (
            <>
              <span className="h-4 w-px bg-hair" />
              <button
                onClick={() => setHint(true)}
                className="rounded px-2 py-1 font-mono text-2xs tracking-normal text-fg-3 transition-colors hover:text-fg"
              >
                keys
              </button>
            </>
          )}
        </div>
      </div>

      {/* Selected-object readout, straight off the SGP4 propagation */}
      {selected && (
        <div className="absolute bottom-3 right-3 w-60 rounded border border-hair bg-ink-900/80 px-3 py-2.5 backdrop-blur-sm">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              {/*
               * Every satellite used to be labelled "Protected asset". The
               * live catalogue contains third-party payloads — TIROS 3 sits
               * there with `owned: false` — so selecting one had the console
               * claim command authority over someone else's spacecraft, which
               * is exactly the claim the rest of the UI is careful not to make.
               */}
              <p className={`eyebrow ${selected.exercise ? "text-caution" : ""}`}>{objectRole(selected)}</p>
              <p className="mt-0.5 truncate text-xs text-fg" title={selected.id}>
                {objectLabel(selected)}
              </p>
            </div>
            <button
              onClick={() => selectRef.current?.(null)}
              aria-label="Clear selection"
              title="Clear selection"
              className="shrink-0 text-fg-3 transition-colors hover:text-fg"
            >
              <IconCrosshair size={12} />
            </button>
          </div>
          <dl className="mt-2 space-y-1 font-mono text-2xs tracking-normal">
            {[
              ["NORAD", selected.norad_id ?? "—"],
              ["altitude", `${num(selected.alt_km, 1)} km`],
              ["speed", `${num(selected.velocity_km_s, 3)} km/s`],
              ["inclination", `${num(selected.inclination_deg, 2)}°`],
              ["operator", selected.operator ?? "—"],
            ].map(([label, value]) => (
              <div key={label} className="flex items-baseline justify-between gap-3">
                <dt className="shrink-0 text-fg-3">{label}</dt>
                <dd className="truncate text-fg-2">{value}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}
    </div>
  );
}
