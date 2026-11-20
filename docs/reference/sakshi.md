# Sakshi reference

Sakshi is the orchestrator's always-running process. `uv run sakshi` starts it in a state directory, by default `~/.local/state/orchestrator/sakshi/` or the one `--state-dir` names: a directory it creates readable by its owner only and refuses to use when it is open to the group or to others. It takes a lock there, so a second start in the same directory is refused, loads its [settings file](#settings-file), lays the seeds the settings give a source for, builds the runtime that starts a review's roles when a repository is reviewed, binds the review socket and prints `Sakshi is running. State directory: <directory>`. It stays until SIGINT or SIGTERM, and then stops in order: it refuses new requests, closes the socket, cancels every review in flight and closes the runtime.

A review request, sent by [`sakshi review`](#the-command), is decided against the settings and recorded once per head commit in the state directory; a request for a commit already recorded is accepted and raises nothing. An accepted review lays a [workspace](#a-reviews-files-and-its-failure), starts the team's roles together, each a session of its seed started as the role's own OS user and all under one deadline, writes the reports of the roles that answered into the workspace, and then starts the musafir over them under a deadline of its own. The review's record in the state directory passes through `accepted`, `running`, `composing` and `finished`, or ends `failed`. This page holds the values of what Sakshi reads and what it answers: its settings file, the review request's fields and typed refusals, the files and the failure of a review, and the exit statuses of `sakshi review`.

## Settings file

Sakshi's settings are one file, `sakshi.json`, in its private state directory, whose directory only its owner can read; the file may therefore hold the Telegram token. A path where nothing exists, not even a link, is an absent file; it means no repository is reviewed, and every review request is refused as a repository that is not reviewed. Every value is validated and none is converted: an invalid file, a key it does not know, a key written twice in one object, a wrong type or a bad value stops Sakshi at start with a message naming the file, the key and the requirement, never a value. So does a file that cannot be read or decoded as UTF-8, one nested too deeply, or one holding a number too large to read; a link at the path that leads nowhere is such a file, not an absent one.

## Settings keys

A key is required unless its row, or the paragraph under its table, says it may be absent.

Top level:

| Key | Meaning |
|---|---|
| `workspace_root` | An absolute path: the directory where a review's checkout and the reviews for the musafir are laid. Required when any repository is listed. |
| `environment` | A list of environment variable names, each `[A-Za-z_][A-Za-z0-9_]*` and none twice, and none of `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_MESSAGING_TOKEN` and `CLAUDE_CODE_SESSION_ID`, which carry the parent session's messaging and identity, so that a role never inherits them. A role is started with those names taken from Sakshi's own environment and nothing else of it. Absent means no names. |
| `telegram_bot_token`, `telegram_chat_id` | The bot's credentials, non-empty strings, as in [Telegram notifications](configuration.md#telegram-notifications). Optional; with either absent no notification is sent. |
| `telegram_alerts` | A list of event types to notify on, strings, as in [Telegram notifications](configuration.md#telegram-notifications). Optional. |
| `repositories` | An object keyed by `owner/name` in the form of [the request](#the-request); two keys that differ only in case are an error. Absent means no repository. |

Per repository:

| Key | Meaning |
|---|---|
| `checkout` | An absolute path: the checkout of the repository on the machine. |
| `team` | The one team that reviews the repository for now, an object, below. |

Per team:

| Key | Meaning |
|---|---|
| `memory` | An absolute path: the folder holding each head's own folder, named by the head's number. |
| `deadline_seconds` | How long Sakshi waits for the roles, one deadline for them all, and again for the musafir: a finite number above 0, never a boolean, with no default. |
| `roles` | An object keyed by role name, a slug: lowercase `[a-z0-9]` runs joined by single hyphens, 1 to 64 characters; each value a role, below. |
| `musafir` | The team's musafir, raised over the roles' reports once they have reported, in the same form as a role. |

A team with no roles, or with no musafir, loads; a review request for its repository is refused as roles or musafir incomplete in the settings. A role missing a field other than `source` is an error.

Per role and for the musafir:

| Key | Meaning |
|---|---|
| `seed` | An absolute path: the seed's directory, the role's working directory. Once laid, the role users read it and never write it; this covers the seed's own directory, including one that existed empty before the clone. Sakshi sets only the seed's own directory: any directory above it that already exists must be traversable by the role users. |
| `user` | The OS user the role runs as: `[a-z_][a-z0-9_-]*`, at most 31 characters, so that it can never read as an option. |
| `model` | The model, a non-empty string. |
| `effort` | The effort: one of `low`, `medium`, `high`, `xhigh`, `max`. |
| `head` | The number of the head the role is, a string of exactly two decimal digits; `<memory>/<head>` is the role's memory folder. Two roles, or a role and the musafir, never name one head. |
| `source` | Optional. The address Sakshi lays the seed from when it starts: a non-empty string with no whitespace and not beginning with `-`, that is an absolute path or an address git reads as remote, a scheme or `host:path` with a colon before the first slash, and carries no credential, which git's own configuration on the stand supplies. A relative path is refused at load, so the clone and the refresh read one repository from any working directory. A seed that already exists is pointed at the configured source and refreshed from it at the start, so a changed `source` takes effect. A role with none is not laid. A `seed` path is the directory it resolves to, so two spellings of one directory are one seed, and two roles on one seed name one source, or only one of them names it. |

A path's existence is not checked when the settings load: a missing seed or checkout fails the review that needs it.

A seed with a source is laid once per distinct path when Sakshi starts, before it binds its socket: an absent path or an empty directory is cloned from the source, a directory holding a repository is fetched and reset to the source's new head, and anything else stops Sakshi naming the path. A seed that cannot be laid stops Sakshi naming the path and git's text. A seed therefore changes for the roles only when Sakshi starts, never while a review runs.

## A review's files and its failure

A review's workspace is a directory of a random name, a `uuid4` in hexadecimal, under `workspace_root`, holding `tree/`, the detached worktree at the head commit, and `reviews/`, one file `<role>.md` for each role that reported, written when the wait for the roles ends. It is removed when the review ends. Only Sakshi's user writes it, and the role users read it. A tree holding anything beyond the head commit, an ignored file included, is a workspace that could not be laid, and a review whose workspace could not be laid fails as that: the cause is mostly the network or the pull request's head, and the person looks there. Sakshi's own storage failing is a failure of another kind, whichever file it was writing: a report that could not be written into the workspace and the record Sakshi keeps of the review.

Sakshi needs git 2.29 or later. A review's fetch of the pull request's head leaves the checkout's `FETCH_HEAD` as it was, which that version introduced, and an earlier git refuses it: every review then fails as a workspace that could not be laid.

A review that fails is reported through [Telegram notifications](configuration.md#telegram-notifications) with the event type `stop` and three lines, `Review failed: <repository>#<number>`, `Reason: <clause>` and `Head commit: <sha>`. The clause is fixed by the reason, and no other text is ever sent:

| Reason | Clause |
|---|---|
| `no-role-reported` | no role reported |
| `musafir-gave-no-output` | the musafir gave no output |
| `stop-cut-the-review` | a stop cut the review |
| `workspace-not-laid` | the workspace could not be laid |
| `storage-failed` | Sakshi could not write the review's files |

## The request

A review request is four fields and no others; an unknown, missing or wrongly typed one is an invalid request.

| Field | Form |
|---|---|
| `repository`, `head_repository` | `owner/name`. The owner is 1 to 39 characters from `[A-Za-z0-9]` joined by single hyphens, so it neither begins nor ends with a hyphen; the name is 1 to 100 characters from `[A-Za-z0-9._-]` and is neither `.` nor `..`. Two spellings that differ only in case name one repository. |
| `number` | A JSON integer from 1 to 2147483647; a boolean is refused. |
| `head_commit` | Exactly 40 characters of lowercase hexadecimal. |

The typed refusals, by their values: `invalid-request`, which the request's parse gives, and `stopping`, `repository-not-reviewed`, `head-from-another-repository` and `roles-incomplete`, which the intake decides in that order once the request parses; the first that applies answers.

## The command

`sakshi review <owner/name> <number> --head-commit <sha> --head-repository <owner/name>` reaches the running process over `review.sock` in its state directory, a Unix socket whose path may not pass 103 bytes on macOS. `--state-dir` names the directory and is accepted before or after `review`. Sakshi refuses to start in a directory whose socket path cannot bind.

The process answers a request with one line: `{"accepted": true}` for an acceptance, exactly that and nothing else, or `{"accepted": false, "refusal": <the refusal's value>}` for a refusal. A reply in neither form is not an answer, and the command treats the door as one that cannot be reached.

| Exit status | Meaning |
|---|---|
| `0` | The request was accepted. |
| `1` | The request was refused, an oversize request as `invalid-request` included; the refusal's value is printed on standard error. |
| `2` | A usage error of the command line. |
| `4` | Nothing listens: Sakshi is not running, its socket cannot be reached, or its reply is not in an answer's form. The reason, naming the socket path, is printed on standard error. |
