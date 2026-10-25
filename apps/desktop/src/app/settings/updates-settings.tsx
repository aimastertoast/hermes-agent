import { useStore } from '@nanostores/react'
import { type ReactElement, useContext } from 'react'

import { saveUpdateSettings } from '@/api/update-settings'
import { forkStrings } from '@/i18n/fork'
import { SlidersHorizontal } from '@/lib/icons'
import { notifyError } from '@/store/notifications'
import { $updateAllowLocalAhead } from '@/store/updates-fork'

import { SectionHeading, SettingsBreadcrumbContext, ToggleRow } from './primitives'

/** Renderer-side toggle for the orchestrator's local-ahead bypass (task 9
 *  atom). It persists via `persistentAtom` — flipping it here survives an app
 *  restart and is what the orchestrator reads on every invoke. Default is off:
 *  the user must opt in once per install.
 *
 *  The "Auto-apply safe updates" switch that used to sit here was removed:
 *  nothing ever read the flag it persisted, so it could only ever look like a
 *  broken updater. */
export function UpdatesSettings(): ReactElement {
  const hasBreadcrumb = useContext(SettingsBreadcrumbContext)
  const u = forkStrings.updateModes
  const allowLocalAhead = useStore($updateAllowLocalAhead)

  // The updater that reads this toggle runs in ANOTHER process and reads
  // `HERMES_HOME/update_settings.json` on every invoke, so a toggle flipped
  // here only takes effect once it has been written there.
  const persistToggles = (): void => {
    void saveUpdateSettings({
      allowLocalAhead: $updateAllowLocalAhead.get()
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
