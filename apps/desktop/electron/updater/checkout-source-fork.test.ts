import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

import { expect, it } from 'vitest'

import { readSourceUpdate } from './checkout-source'

/**
 * FORK-OWNED regression. The About pane's red "couldn't reach the update
 * server" never cleared on this fork because the bridge ran upstream's check
 * entry, which answers a branch that lives only on the fork remote with
 * `error: "branch-local-only"`. The checkout ships
 * `hermes_cli/source_check_fork.py`, so the bridge must run THAT module.
 */
it.skipIf(process.platform !== 'win32')(
  'runs the fork check entry when the checkout ships one, and upstream’s otherwise',
  async (): Promise<void> => {
    const root: string = fs.mkdtempSync(path.join(os.tmpdir(), 'fork-source-check-'))
    const home: string = path.join(root, 'profile')
    const launcher: string = path.join(root, '.hermes', 'bin', 'hermes.cmd')
    fs.mkdirSync(path.dirname(launcher), { recursive: true })
    fs.mkdirSync(path.join(root, 'pm'))
    fs.mkdirSync(path.join(root, 'hermes_cli'))
    fs.mkdirSync(home)
    // Echo which module the launcher was asked to run; the bridge parses the JSON.
    fs.writeFileSync(
      launcher,
      '@echo off\r\nif not "%~1"=="--run-module" exit /b 5\r\necho {"supported":true,"channel":"stable","behind":-1,"module":"%~2"}\r\n'
    )

    const probe = {
      python: 'nonexistent-system-python',
      git: 'git',
      updateRoot: root,
      hermesHome: home,
      channel: 'stable' as const
    }

    const forkModule: string = path.join(root, 'hermes_cli', 'source_check_fork.py')

    try {
      fs.writeFileSync(forkModule, '')
      await expect(readSourceUpdate(probe)).resolves.toMatchObject({ module: 'hermes_cli.source_check_fork' })

      // An upstream checkout has no fork module; upstream's entry must stay the default.
      fs.rmSync(forkModule)
      await expect(readSourceUpdate(probe)).resolves.toMatchObject({ module: 'hermes_cli.source_check' })
    } finally {
      fs.rmSync(root, { recursive: true, force: true })
    }
  }
)
