/**
 * Profile scoping for registry-routed backend requests.
 *
 * Extracted from `main.ts` so that file keeps no fork-local block inside
 * `dispatchRegistryApiRequest`: upstream edits `main.ts` constantly, and a
 * 13-line fork hunk there turned routine upstream edits into merge conflicts
 * that aborted `hermes update`. This module is upstream-unknown, so it cannot
 * conflict — and being a pure function it is unit-testable directly, which the
 * inline version was not (its regression test could only grep main.ts source
 * text, since main.ts imports electron at module scope).
 */

/**
 * A delegated local profile (connectionId 'local' over a genuinely local v1
 * route) lands on the SHARED primary backend — `ensureBackend`'s `sharedPrimary`
 * route, one home serving every profile. Its path must carry the profile scope
 * exactly the way the v1 handler computes it.
 *
 * `pathForRegistryBackendRequest` alone cannot do this: its
 * `translateSelfProfileQuery` only rewrites an EXISTING `?profile=`, so scoped
 * reads like `GET /api/model/info` left unscoped, the shared primary answered
 * from its launch home, and the Settings → 模型 page showed (and then applied)
 * the launch profile's model under every other profile's chip
 * (#118431/#118432 through the registry route).
 *
 * Isolated, ssh, and shared-remote registry backends keep the original
 * behaviour — they either have their own home or are scoped by an existing
 * query parameter.
 */

/** The subset of the connection descriptor this decision needs. */
export interface RegistryPathScope {
  sharedPrimary?: boolean
}

/** Mirrors `resolveProfileApiRequest(profile, path, opts)` in connection-config.ts. */
export type RegistryProfileRouteResolver = (
  profile: string | null,
  path: string,
  routeOptions: unknown
) => { requestPath: string }

export interface ResolveRegistryRequestPathDeps {
  pathForRegistryBackendRequest: (
    path: string,
    profile: string | null,
    connection: RegistryPathScope
  ) => string
  resolveProfileApiRequest: RegistryProfileRouteResolver
  profileRouteOptions: (profile: string | null, request: unknown) => unknown
}

export function resolveRegistryRequestPath(
  path: string,
  profile: string | null,
  connection: RegistryPathScope | null | undefined,
  request: unknown,
  deps: ResolveRegistryRequestPathDeps
): string {
  if (connection?.sharedPrimary) {
    return deps.resolveProfileApiRequest(
      profile,
      path,
      deps.profileRouteOptions(profile, request)
    ).requestPath
  }

  return deps.pathForRegistryBackendRequest(path, profile, connection as RegistryPathScope)
}