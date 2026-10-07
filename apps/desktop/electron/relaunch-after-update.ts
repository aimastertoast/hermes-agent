/**
 * In-app update relaunch.
 *
 * Lives outside `main.ts` so that file carries no fork-local block in the
 * ipcMain registration region: upstream edits `main.ts` constantly, and a
 * 30-line handler of ours sitting between its own registrations turned routine
 * upstream edits into merge conflicts that aborted `hermes update`.
 *
 * The in-place update path rebuilds the desktop bundle while this app stays
 * alive (the in-app suicide-guard refuses to taskkill the parent), so the
 * running renderer keeps the OLD bundle in memory and Settings/About keeps
 * showing "build too old". The renderer invokes this once the update finished
 * so we re-exec Hermes.exe and load the new bundle. `app.relaunch()`
 * re-executes THIS executable (never a taskkill on the parent); the agent
 * backend/gateway/Hindsight are separate processes and survive. Never fires
 * when a detached updater hand-off already owns the relaunch (we'd
 * double-spawn).
 */

import { app, type IpcMain } from 'electron'

export const RELAY_AFTER_UPDATE_CHANNEL = 'hermes:desktop:relaunch-after-update'

export interface RelaunchAfterUpdateDeps {
  /**
   * Whether a hand-off already owns the relaunch. A getter, not a boolean:
   * the owner keeps it in mutable state and reassigns it as the hand-off
   * starts and ends.
   */
  isQuittingForHandoff: () => boolean
  rememberLog: (chunk: unknown) => void
}

export function registerRelaunchAfterUpdate(ipcMain: IpcMain, deps: RelaunchAfterUpdateDeps): void {
  ipcMain.handle(RELAY_AFTER_UPDATE_CHANNEL, async () => {
    if (deps.isQuittingForHandoff()) {
      deps.rememberLog('[updates] relaunch-after-update ignored: already quitting for hand-off')
      return { ok: false as const, reason: 'handoff-in-progress' }
    }

    deps.rememberLog('[updates] relaunch-after-update: re-execing Hermes.exe to load the new bundle')
    try {
      app.relaunch({ args: process.argv.slice(1) })
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      deps.rememberLog(`[updates] relaunch-after-update failed: ${message}`)
      return { ok: false as const, error: message }
    }

    // Reply to the IPC before tearing the window down.
    setTimeout(() => app.quit(), 150)

    return { ok: true as const }
  })
}