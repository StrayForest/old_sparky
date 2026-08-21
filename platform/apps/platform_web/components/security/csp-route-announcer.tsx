"use client";

import { useInsertionEffect } from "react";
import { useCspNonce } from "@/components/security/csp-nonce-provider";

const ANNOUNCER_TAG = "next-route-announcer";
const ANNOUNCER_NAME = "next-route-announcer";
const ANNOUNCER_ID = "__next-route-announcer__";
const ANNOUNCER_CSS = `
  :host { position: absolute; }
  #${ANNOUNCER_ID} {
    position: absolute;
    border: 0;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    clip: rect(0 0 0 0);
    overflow: hidden;
    white-space: nowrap;
    overflow-wrap: normal;
  }
`;

export function CspRouteAnnouncer() {
  const nonce = useCspNonce();

  useInsertionEffect(() => {
    if (!nonce || document.getElementsByName(ANNOUNCER_NAME).length > 0) {
      return;
    }

    // Next's App Router reuses a pre-existing named shadow host. Supplying it
    // before passive effects avoids the framework's two inline style attrs
    // while retaining the same assertive route-change announcement behavior.
    const container = document.createElement(ANNOUNCER_TAG);
    container.setAttribute("name", ANNOUNCER_NAME);
    const announcer = document.createElement("div");
    announcer.id = ANNOUNCER_ID;
    announcer.role = "alert";
    announcer.ariaLive = "assertive";
    const shadowRoot = container.attachShadow({ mode: "open" });
    shadowRoot.appendChild(announcer);

    const style = document.createElement("style");
    style.nonce = nonce;
    style.textContent = ANNOUNCER_CSS;
    shadowRoot.appendChild(style);
    document.body.appendChild(container);
  }, [nonce]);

  return null;
}
