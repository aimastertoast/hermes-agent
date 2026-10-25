import { useStore } from '@nanostores/react'
import { type ReactElement, useContext } from 'react'

import { saveUpdateSettings } from '@/api/update-settings'
import { forkStrings } from '@/i18n/fork'
import { SlidersHorizontal } from '@/lib/icons'
import { notifyError } from '@/store/notifications'
import { $updateAllowLocalAhead, $updateSafeModeAuto } from '@/store/updates'

import { SectionHeading, SettingsBreadcrumbContext, ToggleRow } from './primitives'

/** Renderer-side toggles for the orchestrator's user-facing update modes
 *  (task 9 atoms). Both persist via `persistentAtom` — flipping one here
 *  survives an app restart and is what the orchestrator reads on every
 *  invoke. Defaults are off: the user must opt in once per mode. */
export function UpdatesSettings(): ReactElement {
  const hasBreadcrumb = useContext(SettingsBreadcrumbContext)
  const u = forkStrings.updateModes
  const safeModeAuto = useStore($updateSafeModeAuto)
  const allowLocalAhead = useStore($updateAllowLocalAhead)

  // The updater that reads these toggles runs in ANOTHER process and reads
  // `HERMES_HOME/update_settings.json` on every invoke, so a toggle flipped
  // here only takes effect once it has been written there. Both toggles go
  // out together — the file is read whole, so flipping one must not reset
  // the other to its default.
  const persistToggles = (): void => {
    void saveUpdateSettings({
      allowLocalAhead: $updateAllowLocalAhead.get(),
      safeModeAuto: $updateSafeModeAuto.get()
    }).catch((error: unknown) => {
      notifyError(error, u.saveFailed)
    })
  }

  return (
    <section aria-labelledby="updates-settings-heading" className={hasBreadcrumb ? undefined : 'mt-8'}>
      <SectionHeading icon={SlidersHorizontal} title={u.title} />
      <div className="mx-auto w-full max-w-2xl">
        <h2 className="sr-only" id="updates-settings-heading">
          {u.title}
        </h2>
        <ToggleRow
          checked={safeModeAuto}
          description={u.safeModeAutoDescription}
          id="update-safe-mode-auto"
          label={u.safeModeAutoLabel}
          onChange={next => {
            $updateSafeModeAuto.set(next)
            persistToggles()
          }}
        />
        <ToggleRow
          checked={allowLocalAhead}
          description={u.allowLocalAheadDescription}
          id="update-allow-local-ahead"
          label={u.allowLocalAheadLabel}
          onChange={next => {
            $updateAllowLocalAhead.set(next)
            persistToggles()
          }}
        />
      </div>
    </section>
  )
}
