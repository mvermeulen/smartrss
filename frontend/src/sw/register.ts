/**
 * Service Worker registration & user-gated update hook (1c §7.2, U-14).
 * Skips registration silently on non-secure origins (e.g. plain HTTP LAN fallback).
 */

export interface SwRegistrationHandle {
  applyUpdate: () => void;
}

/**
 * Activate a waiting Service Worker (or trigger `reg.update()` and wait up to 5 s for
 * `updatefound` -> `waiting`) and return `true` once `controllerchange` fires (`U-08-F1`).
 */
export async function activateWaitingServiceWorker(timeoutMs = 5_000): Promise<boolean> {
  const swContainer = globalThis.navigator?.serviceWorker;
  if (!swContainer || !swContainer.controller) {
    return false;
  }
  try {
    const reg = await swContainer.getRegistration();
    if (!reg) {
      return false;
    }
    const activateWorker = (worker: ServiceWorker): Promise<boolean> =>
      new Promise((resolve) => {
        const onController = () => {
          swContainer.removeEventListener("controllerchange", onController);
          resolve(true);
        };
        swContainer.addEventListener("controllerchange", onController);
        worker.postMessage({ type: "SKIP_WAITING" });
        globalThis.setTimeout(() => {
          swContainer.removeEventListener("controllerchange", onController);
          resolve(false);
        }, timeoutMs);
      });

    if (reg.waiting) {
      return activateWorker(reg.waiting);
    }

    const foundPromise = new Promise<ServiceWorker | null>((resolve) => {
      const timer = globalThis.setTimeout(() => resolve(null), timeoutMs);
      const onUpdateFound = () => {
        const installing = reg.installing;
        if (!installing) return;
        installing.addEventListener("statechange", () => {
          if (installing.state === "installed" && reg.waiting) {
            globalThis.clearTimeout(timer);
            resolve(reg.waiting);
          }
        });
      };
      reg.addEventListener("updatefound", onUpdateFound, { once: true });
    });

    await reg.update().catch(() => undefined);
    const waiting = reg.waiting ?? (await foundPromise);
    if (waiting) {
      return activateWorker(waiting);
    }
    return false;
  } catch {
    return false;
  }
}

export async function registerServiceWorker(
  onUpdateAvailable: (applyUpdate: () => void) => void,
): Promise<SwRegistrationHandle | null> {
  if (typeof window === "undefined" || !window.isSecureContext || !("serviceWorker" in navigator)) {
    return null;
  }

  try {
    // Only reload on controllerchange when upgrading an ALREADY-controlled page (not on first install claim)
    const hadController = Boolean(navigator.serviceWorker.controller);
    const reg = await navigator.serviceWorker.register("/sw.js", { scope: "/" });

    let reloading = false;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (hadController && !reloading) {
        reloading = true;
        window.location.reload();
      }
    });

    const triggerApply = () => {
      const waiting = reg.waiting;
      if (waiting) {
        waiting.postMessage({ type: "SKIP_WAITING" });
      } else {
        window.location.reload();
      }
    };

    if (reg.waiting && navigator.serviceWorker.controller) {
      onUpdateAvailable(triggerApply);
    }

    reg.addEventListener("updatefound", () => {
      const installing = reg.installing;
      if (!installing) return;
      installing.addEventListener("statechange", () => {
        if (installing.state === "installed" && navigator.serviceWorker.controller) {
          onUpdateAvailable(triggerApply);
        }
      });
    });

    return { applyUpdate: triggerApply };
  } catch {
    return null;
  }
}
