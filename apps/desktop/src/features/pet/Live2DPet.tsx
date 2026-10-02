import { useEffect, useRef, useState } from "react";
import * as PIXI from "pixi.js";
import { getCurrentWindow } from "@tauri-apps/api/window";
import "./live2d.css";

type Props = { onInteract?: () => void };

let coreLoading: Promise<void> | undefined;
const loadLocalCore = () => {
  if ((window as unknown as { Live2DCubismCore?: unknown }).Live2DCubismCore) return Promise.resolve();
  if (!coreLoading) coreLoading = new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "/models/live2d/live2dcubismcore.min.js";
    const timer = window.setTimeout(() => { script.remove(); reject(new Error("Local Live2D Core load timed out")); }, 5000);
    script.onload = () => {
      window.clearTimeout(timer);
      if ((window as unknown as { Live2DCubismCore?: unknown }).Live2DCubismCore) resolve();
      else reject(new Error("Local Live2D Core is not installed"));
    };
    script.onerror = () => { window.clearTimeout(timer); script.remove(); reject(new Error("Optional Live2D Core is not installed")); };
    document.head.appendChild(script);
  });
  return coreLoading;
};

export function Live2DPet({ onInteract }: Props) {
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const onInteractRef = useRef(onInteract);
  const [fallback, setFallback] = useState(false);
  const press = useRef<{ x: number; y: number } | null>(null);
  const suppressTapUntil = useRef(0);
  const presentation = useRef({ phase: "idle", paused: false, mouth: 0, audioActive: false });
  useEffect(() => {
    const voice = (event: Event) => { const value = (event as CustomEvent).detail; if (value?.phase) { presentation.current.phase = value.phase; presentation.current.paused = Boolean(value.paused); } };
    const audio = (event: Event) => { const value = (event as CustomEvent).detail; presentation.current.mouth = Number.isFinite(value?.value) ? Math.max(0, Math.min(1, value.value)) : 0; presentation.current.audioActive = Boolean(value?.active); };
    window.addEventListener("ella-voice-status", voice);
    window.addEventListener("ella-pet-audio", audio);
    window.dispatchEvent(new Event("ella-voice-status-request"));
    return () => { window.removeEventListener("ella-voice-status", voice); window.removeEventListener("ella-pet-audio", audio); };
  }, []);
  onInteractRef.current = onInteract;

  const pointerDown = (event: React.PointerEvent<HTMLElement>) => {
    if (event.button !== 0) return;
    press.current = { x: event.clientX, y: event.clientY };
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const pointerMove = (event: React.PointerEvent<HTMLElement>) => {
    const start = press.current;
    if (!start || Math.hypot(event.clientX - start.x, event.clientY - start.y) < 7) return;
    press.current = null;
    suppressTapUntil.current = Date.now() + 600;
    if ("__TAURI_INTERNALS__" in window) void getCurrentWindow().startDragging();
  };
  const pointerUp = () => { press.current = null; };
  const interact = () => {
    if (Date.now() >= suppressTapUntil.current) onInteractRef.current?.();
  };

  useEffect(() => {
    let active = true;
    let application: PIXI.Application | null = null;
    let model: any = null;
    let timer: number | undefined;
    let timedOut = false;

    const start = async () => {
      if (!canvas.current) return;
      try {
        await loadLocalCore();
        if (!active || !canvas.current) return;
        (window as unknown as { PIXI: typeof PIXI }).PIXI = PIXI;
        const { Live2DModel } = await import("pixi-live2d-display/cubism4");
        try { Live2DModel.registerTicker(PIXI.Ticker); } catch { /* already registered */ }
        if (!active || !canvas.current) return;
        const app = new PIXI.Application({
          view: canvas.current,
          width: 220,
          height: 500,
          backgroundAlpha: 0,
          antialias: true,
          resolution: window.devicePixelRatio || 1,
          autoDensity: true,
        });
        application = app;
        const loaded = Live2DModel.from("/models/live2d/Hiyori/Hiyori.model3.json", { autoInteract: true })
          .then((pet) => { if (timedOut || !active) pet.destroy(); return pet; });
        const timeout = new Promise<never>((_, reject) => {
          timer = window.setTimeout(() => { timedOut = true; reject(new Error("Live2D load timed out")); }, 5000);
        });
        const pet = await Promise.race([loaded, timeout]);
        if (timer !== undefined) window.clearTimeout(timer);
        if (!active) return;
        model = pet;
        let smoothedMouth = 0;
        pet.internalModel.on("beforeModelUpdate", () => {
          const state = presentation.current;
          const core = pet.internalModel.coreModel as { setParameterValueById(id: string, value: number): void; addParameterValueById(id: string, value: number): void };
          const target = state.audioActive ? state.mouth : 0;
          smoothedMouth += (target - smoothedMouth) * 0.45;
          core.setParameterValueById("ParamMouthOpenY", smoothedMouth);
          if (!state.paused && state.phase === "recording") core.addParameterValueById("ParamAngleZ", -3);
          if (state.phase === "waiting") { core.addParameterValueById("ParamAngleZ", 4); core.addParameterValueById("ParamEyeBallY", 0.12); }
        });
        const scale = Math.min((320 * 0.95) / pet.width, (520 * 0.95) / pet.height);
        pet.scale.set(scale);
        pet.anchor.set(0.5, 0.5);
        pet.x = 110;
        pet.y = 250;
        pet.on("pointertap", () => {
          if (Date.now() < suppressTapUntil.current) return;
          try { pet.motion("TapBody"); } catch { pet.motion("Idle"); }
          interact();
        });
        app.stage.addChild(pet);
      } catch (error) {
        console.info("Optional Live2D unavailable, using the original SVG portrait", error);
        try { application?.destroy(true, { children: true, texture: true }); } catch { /* renderer may be partial */ }
        application = null;
        if (active) setFallback(true);
      }
    };

    void start();
    return () => {
      active = false;
      timedOut = true;
      if (timer !== undefined) window.clearTimeout(timer);
      try { model?.destroy(); } catch { /* renderer may already be closed */ }
      try { application?.destroy(true, { children: true, texture: true }); } catch { /* same */ }
    };
  }, []);

  return fallback ? (
    <button className="pet-portrait" onClick={interact} onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={pointerUp} onPointerCancel={pointerUp} aria-label="点击对话，拖动艾拉">
      <img src="/ella-placeholder.svg" alt="艾拉原创 2D 占位立绘" draggable={false} />
    </button>
  ) : <canvas className="pet-live2d" ref={canvas} onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={pointerUp} onPointerCancel={pointerUp} aria-label="点击对话，拖动艾拉 Live2D 桌宠" />;
}
