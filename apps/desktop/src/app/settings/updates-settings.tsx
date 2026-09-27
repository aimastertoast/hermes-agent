import { useStore } from '@nanostores/react'
import { type ReactElement, useContext } from 'react'

import { useI18n } from '@/i18n'
import { SlidersHorizontal } from '@/lib/icons'
import { $updateAllowLocalAhead, $updateSafeModeAuto } from '@/store/updates'

import { SectionHeading, SettingsBreadcrumbContext, ToggleRow } from './primitives'

/** Renderer-side toggles for the orchestrator's user-facing update modes
 *  (task 9 atoms). Both persist via `persistentAtom` — flipping one here
 *  survives an app restart and is what the orchestrator reads on every
 *  invoke. Defaults are off: the user must opt in once per mode. */
export function UpdatesSettings(): ReactElement {
  const hasBreadcrumb = useContext(SettingsBreadcrumbContext)
  const { t } = useI18n()
  const u = t.settings.about.updateModes
  const safeModeAuto = useStore($updateSafeModeAuto)
  const allowLocalAhead = useStore($updateAllowLocalAhead)

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
          onChange={next => $updateSafeModeAuto.set(next)}
        />
        <ToggleRow
          checked={allowLocalAhead}
          description={u.allowLocalAheadDescription}
          id="update-allow-local-ahead"
          label={u.allowLocalAheadLabel}
          onChange={next => $updateAllowLocalAhead.set(next)}
        />
      </div>
    </section>
  )
}
