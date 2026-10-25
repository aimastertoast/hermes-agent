import { hermesApi } from './client'

/** Persist the desktop's update-behavior toggles for the updater.
 *
 *  These live in their own module for the same reason `./update-receipts` does:
 *  upstream `system.ts` is edited often, and a fork-only block inside it turns
 *  every routine upstream reformat into a merge conflict that aborted
 *  `hermes update`. A brand-new file upstream does not touch can never conflict.
 *
 *  The updater runs in another process and reads `HERMES_HOME/update_settings.json`
 *  on every invoke (see `hermes_cli.update_settings`). The renderer kept the
 *  toggles in its own local storage instead, so the file was never written and
 *  the gate the toggles arm stayed unreachable from the UI. */

/** The toggles, as the renderer holds them. */
export interface UpdateSettingsToggles {
  /** Let an update proceed past the local-ahead refusal. */
  allowLocalAhead: boolean
}

/** Write the toggles to the backend's update-settings file.
 *
 *  The file is read whole, so the payload always carries every toggle the
 *  renderer owns. */
export function saveUpdateSettings(toggles: UpdateSettingsToggles): Promise<{ ok: boolean }> {
  return hermesApi<{ ok: boolean }>({
    path: '/api/hermes/update/settings',
    method: 'POST',
    body: {
      allow_local_ahead: toggles.allowLocalAhead
    }
  })
}
