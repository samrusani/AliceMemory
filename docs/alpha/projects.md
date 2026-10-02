# Projects

Unreleased (on main, not in v0.20.0): Alice can work out which git repository a folder belongs to, `alice-memory project` shows what it found, and, with per-project scoping switched on, the session brief and `alice_resume` follow the project. Scoping is **off by default**, so with nothing set every output is what v0.20.0 printed, byte for byte. This is the first part of per-project memory. Writes do not change yet: a note is saved the same way in every folder, as in v0.20.0, and `alice_recall` and `alice_context_pack` read the whole vault as before. In v0.20.0 `alice-memory project` is not a command: `alice-memory project show` is read as arguments to `alice-memory mcp` and fails with `invalid_request`.

Every statement below about the project commands, the switch and the project brief is Unreleased (on main, not in v0.20.0).

## Try it

Scoping is off until you turn it on. Either of these turns it on:

```bash
export ALICE_PROJECT_SCOPING=on           # for one shell, or in a host entry's env map
alice-memory project scoping on           # once, saved in the vault
```

Then start a session in a git repository, or run `alice-memory brief` inside one. `alice-memory project show` tells you what Alice found for a folder, and `alice-memory project scoping status` says whether scoping is on and why. To turn it off again, `alice-memory project scoping off` (or `ALICE_PROJECT_SCOPING=off`). The environment variable beats the vault's setting.

## The project brief

With scoping on and a project found, the brief that the SessionStart hook injects and that `alice-memory brief` prints changes in four ways.

1. After the frame line it opens with one line that Alice writes (not a stored note): the project's label in quotes, its id, where it was found (`the git remote` or `the repository folder`) and the id to pass as `project_scope` if your Alice tools cannot see the folder.
2. This project's notes come first. A note is in the project when its project scope holds one of the project's ids. The newest eight facts and eight open loops are filled with the project's notes first and notes that belong to no project after them, and a quarter of the places, rounded down (two of eight), are kept for the global ones whenever the project has more than enough and global has any. At a limit of three or fewer nothing is kept back, so a caller who asks for one item gets the project's.
3. A note that belongs to no project is marked: `**fact** (global): "..."`. A note belongs to no project when its project scope holds no Alice project id: it is empty, or it holds only free-form names such as `Alice`. Every note saved before the upgrade is global, and so is every note an agent filed under a name it chose, so none of them disappears from any project.
4. Notes of another project never show.

Project briefs no longer automatically include global family, health, spiritual, legal or financial material. This applies when Alice finds a project for the folder. When no project is found, when detection fails, or when project scoping is off, the brief searches all memory and still includes them.

The rule applies to every section of the brief: facts, open loops, the recent-change merges, the source excerpts, and the notes and sources the excerpt query is built from. It applies to global notes only: a note of this project in one of those five domains still shows, because the rule is about material that follows a person into every project. It reads the stored domain label (`SENSITIVE_DOMAINS`, the same set that makes a commit ask for confirmation) and does not catch a note filed under another label, such as a health note filed as `personal`. The same rule holds for `alice_resume` when it names no project, because on a host with no SessionStart hook (Hermes, OpenCode) it is the agent's first call. It does not hold for `alice_recall`, `alice_context_pack`, `alice_recent_decisions` or the open loop list, which return these notes under the permissions and sensitivity limits they already apply, and it does not hold for `alice-memory brief --scope global` or `--scope all`, which are you asking. The brief says nothing about how many notes it held back, because a count would be metadata about the material the rule keeps out.

### Where the folder comes from

The hook takes the start folder from the first of these that is an absolute folder that exists: `--project-dir`, `ALICE_PROJECT_DIR`, the `cwd` string in the JSON the host sends on stdin, then the hook's working folder. Claude Code 2.1.281 and Codex 0.158.0 send the folder the session started in as `cwd`, which the host-evidence run recorded, and the resolver walks up from it to the git root, so a session started in a subfolder finds its repository. The hook reads at most 64 KiB of stdin, reads and discards the rest so the host's write never blocks, and ignores a `cwd` that is not an absolute path. `alice-memory brief` and `alice-memory sleep-proposals` take `--project-dir PATH`, and `alice-memory mcp --project-dir PATH` gives the MCP server its folder for `alice_resume`. A server that serves many projects (one global entry) cannot know the current one and falls back to the whole vault.

### Flags

`alice-memory brief` and `alice-memory sleep-proposals` take `--scope`.

- `project` (the default): the project view above. With no project found it is the whole vault plus a status line.
- `project_only`: this project's notes and nothing else. With no project found the command exits with an error and names `--scope all`, because answering a question about one project with every note is the failure this feature exists to prevent.
- `global`: notes that belong to no project, held-back ones included.
- `all`: every note, as before per-project memory, held-back ones included.

With scoping off the two flags are accepted and ignored, so a script that passes them works either way. `alice-memory sleep` always writes the same sidecar of proposals from the oldest sources of the whole vault, whichever folder it runs in. `sleep-proposals` is framed like a brief and takes the same view: in a project it lists this project's and global sources, leaves out global sources in the five domains, and its `rows not shown` line counts neither those nor another project's sources.

### When there is no project

The brief says why nothing is filtered, in one plain line after the frame, so a miss never looks like a successful filter.

| Line | When |
| --- | --- |
| `No project detected; searching all memory.` | scoping is on and the folder is not in a git work tree |
| `Project detection failed; searching all memory.` | a git directory was found and could not be read within the limits (a `.git` file over 4 KiB or malformed, a config over 256 KiB, missing, unreadable or using `include`), or the resolver raised. The hook still exits 0 |
| `Project scoping is off; searching all memory.` | someone turned scoping off on purpose, with `ALICE_PROJECT_SCOPING=off` or `alice-memory project scoping off` |

The release default prints no line, so with nothing set the brief is what v0.20.0 printed. In all three cases every note is read, held-back ones included, as before.

### What does not change

- Nothing is written differently. A commit or capture without a project scope stores no project, in every folder.
- `alice_recall`, `alice_context_pack`, `alice_recent_decisions` and `alice_open_loops` have no `scope` argument yet and read what they read before. A caller that names `projects`, `project` or `project_scope` keeps that meaning everywhere, `alice_resume` included.
- `~global` is a reserved name Alice uses inside a request. It is refused on every tool when a caller sends it as a project, a project scope or an identity's project scope. It is never stored.
- No table, column or index is added, so a v0.20.0 binary opens a vault this release has read, and a backup from one restores in the other. The project is derived from the folder on every call and stored nowhere.

### What it costs

The brief reads the project's facts and the global facts, and the project's open loops and the global ones, in one pass over the vault each, with no index. Measured on a synthetic vault of 5,000 and of 50,000 notes (`scripts/measure_project_view.py`, CPU time of one Python process on one Mac, median of seven runs with the cases timed in turn): the whole brief (opening the vault, the facts, the open loops, the recent changes and the source excerpts) takes 68 ms unscoped and 83 ms in a project at 5,000 notes, so the project view adds about 15 ms, and 579 ms unscoped and 757 ms in a project at 50,000 notes, so it adds about 178 ms. Of the 757 ms, 359 ms is the source excerpt search, which this change does not touch, and 213 ms is the facts read. The first figure is inside the 100 ms the budget allowed. The second is 28 ms over the 150 ms budget, and it is the number to know if your vault has tens of thousands of notes. Two ordinary queries (one for the project, one for the global notes) cost 22 ms and 241 ms more, so the one-pass read is the better shape at both sizes. `alice_resume` in a project costs 11 ms more at 5,000 notes (46 ms to 57 ms) and 82 ms more at 50,000 (362 ms to 443 ms), because it reads in two queries. The resolver takes 0.08 ms. With the project view's tuple put in the call, `alice_recall` and `alice_context_pack` cost 6 ms or less more at both sizes, which is a measurement for the later slice that gives them the view and not a change in this one. The numbers describe a synthetic vault on one machine: 15 percent of the notes carry one of three project ids, a tenth of the rest are filed under a free-form name, and five percent of the global notes are in the five sensitive domains. They show the shape and rough size and not what your vault will do. Run `scripts/measure_project_view.py build` and `measure` on your own machine to get yours.

## What a project is

A project is the git repository a folder is in. Alice finds it by reading files in the git directory. It starts no process, makes no network call and does not run `git`.

1. The start folder is the first of `--project-dir`, `ALICE_PROJECT_DIR` and the working folder that is an absolute folder that exists. A relative path, or a folder that is missing, is skipped and the next source is tried. On Windows an absolute path is a drive letter and a slash or backslash, or a UNC path. A Windows path handed to a process on Linux is not absolute there.
2. Alice resolves the start folder to its real path, so a symlink into a subfolder finds the repository. It looks for a `.git` entry in that folder and in the folders above it, at most 32 folders in all, the start folder counted. It stops with no project at the home folder (`HOME`, or `USERPROFILE` on Windows) and at the top of the filesystem or drive, and it never reads a `.git` in either. A repository at the home folder, which dotfile setups use, is never a project. The nearest `.git` wins, so a submodule or a vendored clone is its own project.
3. A `.git` directory is the git directory. A `.git` file (at most 4 KiB, first line `gitdir: <path>`) names it. If the git directory holds a `commondir` file, that names the common directory. So a linked worktree has the same project as its main checkout, and a submodule has its own.
4. Alice reads `config` in the common directory (at most 256 KiB, `[remote]` and `[core]` sections only). The remote is `origin`. If there is no `origin` with a URL, it is the only remote that has a URL. A remote URL with no host (a local path, `file://`, a Windows drive path) counts as no remote.

## The id and the label

The id is `prj_` and 16 lowercase hex characters, taken from a SHA-256 hash. Alice derives it each time from the folder. It allocates nothing and keeps no registry, so there is nothing to lose in a restore, and two machines that see the same remote derive the same id.

- With a remote, the primary id is a hash of the normalized remote URL, and a second id is a hash of the real path of the common git directory. The first id listed is the primary one. The second exists so that a note written under the path id before the repository had a remote is not orphaned when a later release reads by project.
- With no remote, the one id is the hash of the real path of the common git directory.
- The URL is normalized to `host[:port]/path`. The scheme, user, password, query and fragment are dropped, the host is lowercased, a trailing `.git` and slashes are dropped, and `git@host:org/repo.git` and `https://host/org/repo` give one string. The owner and repository name are lowercased for github.com, gitlab.com and bitbucket.org only. A port is kept unless it is the default of the URL's own scheme (22 for ssh, 443 for https, 80 for http, 9418 for git). `url.<base>.insteadOf` rewrites are not applied.
- The path is lowercased when the repository's own config says `ignorecase = true`, which git writes on a case-insensitive filesystem, and always on Windows.
- Alice never stores or logs the raw URL or a path, and never prints a URL or a path it found (`project show` prints only the folder you typed). An https remote can carry a token, so the URL is hashed after the credentials are stripped.

The label is the last segment of the normalized URL, or else the name of the folder that holds the git directory (a bare repository: its name without `.git`). Every character outside letters, digits, `.`, `_`, `+` and `-` becomes `-`, and the label is cut at 40 characters, so it is one token. It is text from a folder or repository name, so a label made of plain words survives as a hyphenated phrase. Anything that prints a label quotes it.

### What this costs

- Two repositories on one host at different ports are two projects, and so is one repository reached as `ssh://host:2222/o/r` and as `https://host/o/r`. A self-hosted GitLab or Gitea often serves ssh on a port such as 2222 and https on 443, so cloning one repository both ways gives two projects. The alternative, ignoring ports, would merge two different repositories, and a wrong merge exposes notes where a wrong split only hides them.
- A repository with no remote is identified by the real path of its git directory. Moving the folder gives it a new id. A second checkout path, a container bind mount, and a Windows folder seen from WSL are different projects. The remedy is a remote.
- A remote that is renamed or transferred is a different project. A repository whose remote is removed falls back to the path id, and notes saved under the remote id are not found.
- A repository cloned twice on one machine shares the remote id. The two clones' path ids differ, so notes written under a path id before a remote existed are visible in one clone only.

### What a folder can claim

The remote is not authenticated. A crafted `.git/config` that copies the remote URL of another repository gets that repository's id, because that is the same evidence a legitimate reclone presents. Project scoping, when it is on, is a default that prevents accidental mixing. It is not access control: every agent on a keyless install runs as the same operating-system user and can read the same file. A hard wall is a separate data directory.

## When there is no project

`project show` names one of three outcomes.

- `found`: a project, with its label and ids.
- `none`: the folder is not in a git work tree. That covers the home folder, a drive root, a folder inside a bare repository's own directory, a folder that does not exist, a relative path, and a repository whose root is the home folder.
- `failed`: a git directory was found and could not be read within the limits (a `.git` file over 4 KiB or without a `gitdir:` line, a config over 256 KiB, a config that is missing, unreadable or not valid git config, a config that uses `include` or `includeIf`, which Alice does not follow, or a `.git` entry that is neither a file nor a folder), or the resolver raised. `show` says which, in a fixed phrase. Alice never falls back to a path id on a failure, because a repository that does have a remote would get a different id by accident.

## Commands

All three work on the vault chosen by `--data-dir` or `--db`, like every `alice-memory` command.

```bash
alice-memory project show [--project-dir PATH] [--json]
alice-memory project report [--json]
alice-memory project scoping on|off|status
```

`show` prints the project label, the id or ids, what the project was found from (the git remote, or the repository path), which start-folder source was used, and the scoping switch. It prints the folder you typed or set in `--project-dir` or `ALICE_PROJECT_DIR`, escaped and cut if it is long, and never a path it found. It never prints a remote URL. It opens the vault read-only and does not create it.

`report` counts the vault's notes by project, for memories (active and accepted, not deleted), sources (not deleted) and open loops (open). For each kind it prints how many are global, meaning the scope holds no Alice project id (an empty scope, or only free-form names), and how many are in an Alice project. It lists each Alice project id with the label recorded on its notes and its counts, and each free-form project name with its count. A free-form name is an agent's own string, such as `Alice`. A name that looks like a path, a URL or a credential is not printed, and the report says how many were withheld. At most 50 projects and 50 names are listed, with a count of the rest. It writes nothing. It is how you see how much of a vault is still unassigned. Alice does not sort old notes into projects: a note saved before this feature carries no project id.

`scoping status` prints the switch and where it comes from. `scoping on` and `scoping off` save the switch in the vault.

## The scoping switch

The effective switch is, strongest first:

1. `ALICE_PROJECT_SCOPING`, `on` or `off`, in the environment. Any other value is ignored.
2. The vault's `project_scoping` setting, written by `project scoping on|off`.
3. The release default. On main it is off, until the release that turns per-project memory on. The default prints no status line anywhere; only an owner's choice to switch it off does.

Most hosts cannot be given an environment variable per entry, so the setting is saved in the vault. The brief, the SessionStart hook, `alice-memory sleep-proposals` and `alice_resume` read the switch when they start; see [the project brief](#the-project-brief). Nothing else reads it yet.

Changing the setting appends one `scoping.changed` event (the setting name, the new value and the previous value, with no path, URL or project id), and setting the value the vault already holds writes nothing. The setting lives in a table that an export does not carry, and the event does, so a restore keeps your choice. See [Backup and restore](backup-and-restore.md).

## Windows

The resolver takes the platform as a parameter, so the Windows rules can be checked on any machine. A drive letter and a slash or backslash, or a UNC path, is absolute (the `\\?\` and `\\.\` device prefixes are not). Either slash works, the drive letter's case does not matter, the home folder comes from `USERPROFILE`, and the path hash is lowercased. A WSL path such as `/mnt/c/Users/me/repo` is a Linux path to a Linux process and is handled as one.
