import { invoke } from "@tauri-apps/api/core";
import { useState } from "react";
import { Live2DPet } from "./Live2DPet";
import { VoiceControl } from "./VoiceControl";
import { CompanionControl } from "./CompanionControl";
import "./pet.css";

export function PetWindow() {
  const [menuOpen, setMenuOpen] = useState(false);
  const openAdmin = async () => {
    if ("__TAURI_INTERNALS__" in window) await invoke("show_admin");
    else window.open("/?window=admin", "_blank");
  };

  return <main className="pet-window" onContextMenu={(event) => { event.preventDefault(); setMenuOpen((open) => !open); }}>
    <div className="pet-stage" aria-label="艾拉 2D 桌宠">
      <Live2DPet onInteract={() => window.dispatchEvent(new Event("ella-toggle-voice"))} />
    </div>
    <VoiceControl />
    <CompanionControl menuOpen={menuOpen} onCloseMenu={() => setMenuOpen(false)} onOpenAdmin={() => void openAdmin()} />
  </main>;
}
