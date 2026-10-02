# Projects

Unreleased (on main, not in v0.20.0): Alice can work out which git repository a folder belongs to, and `alice-memory project` shows what it found. This is the first slice of per-project memory. Nothing reads or writes notes by project yet, so every note is still saved and read the same way in every folder, as in v0.20.0. In v0.20.0 `alice-memory project` is not a command: `alice-memory project show` is read as arguments to `alice-memory mcp` and fails with `invalid_request`.

Every statement below about the project commands is Unreleased (on main, not in v0.20.0).

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
3. The release default. On main it is off, until the release that turns per-project memory on.

Most hosts cannot be given an environment variable per entry, so the setting is saved in the vault. Nothing reads the switch yet.

Changing the setting appends one `scoping.changed` event (the setting name, the new value and the previous value, with no path, URL or project id), and setting the value the vault already holds writes nothing. The setting lives in a table that an export does not carry, and the event does, so a restore keeps your choice. See [Backup and restore](backup-and-restore.md).

## Windows

The resolver takes the platform as a parameter, so the Windows rules can be checked on any machine. A drive letter and a slash or backslash, or a UNC path, is absolute (the `\\?\` and `\\.\` device prefixes are not). Either slash works, the drive letter's case does not matter, the home folder comes from `USERPROFILE`, and the path hash is lowercased. A WSL path such as `/mnt/c/Users/me/repo` is a Linux path to a Linux process and is handled as one.
