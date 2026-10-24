/**
 * The settings "Applies to" scope for the config-record hooks — FORK-OWNED.
 *
 * Upstream's `use-config-record.ts` reads and writes the config record of the
 * ACTIVE gateway. A settings surface that edits a NAMED profile (the shared
 * "Applies to" scope) must read and write THAT profile's record: writing the
 * active gateway's row with another profile's record paints the wrong config
 * into the active profile's panel (the B1 class).
 *
 * Upstream's own `settings/config-settings.tsx` already does this by hand
 * (`useHermesConfigRecord(scopeProfile)` + `hermesConfigCacheWriter(scopeProfile)`).
 * The font pickers did not, and every line they carried for it was a line
 * upstream had to edit in the same place -- a conflict that aborts
 * `hermes update`. This module gives the pickers the same behavior behind
 * upstream's own names, so they keep upstream's bytes and only two lines
 * change: the import path, and the cache-write call, which now hands the read
 * origin over so the record lands in the row it was read from.
 */
import { useStore } from '@nanostores/react'

import { $settingsRequestProfile } from '@/store/settings-scope'

import {
  hermesConfigCacheWriter,
  useHermesConfigRecord as useHermesConfigRecordUpstream
} from './use-config-record'

/** The value upstream's cache writer accepts. */
type HermesConfigCacheNext = Parameters<ReturnType<typeof hermesConfigCacheWriter>>[0]

/** The read origin upstream's hook hands back as `writeScope`. */
type ConfigReadOrigin = NonNullable<ReturnType<typeof useHermesConfigRecordUpstream>['writeScope']>

/** Upstream's read hook, scoped to the profile the settings pages are editing. */
export const useHermesConfigRecord = () => {
  const scope = useStore($settingsRequestProfile)

  return useHermesConfigRecordUpstream(scope)
}

/**
 * Upstream's cache write, scoped to the profile the record was READ under.
 *
 * `origin` is the read origin (`writeScope`) of the record being written: it
 * names the profile the value belongs to, and it is the only thing that still
 * names it after the settings scope has moved on. The scope's own value is the
 * fallback for a record read before its origin was bound; `undefined` (a
 * "custom" home outside `profiles/`) keeps the unscoped slot, which is the
 * slot the read used.
 *
 * The scope cannot be read from the store here: the write lands after the
 * save round-trips, and by then the settings scope may have moved to another
 * profile -- the record would then be painted into the wrong profile's row.
 */
export const setHermesConfigCache = (next: HermesConfigCacheNext, origin?: ConfigReadOrigin): void => {
  const scope = $settingsRequestProfile.get()
  const profile = scope === undefined ? undefined : (origin?.profile ?? scope)

  hermesConfigCacheWriter(profile)(next)
}
