# Fault handling

When a run cannot proceed, the first question is whose failure it is: the work's, the machine's, or the boundary of an agent's authority. Everything else follows from that answer. See [outcomes.md](outcomes.md) for the outcome axis this classification feeds.

## What counts as a verdict

An agent's output is a verdict only when it is a judgment about the work. A report of the transport dying is not a judgment about the work, whatever channel carries it and however much output preceded it — an agent that never answered has not answered, and text describing why it could not answer is not an answer. How much output arrived before the failure is evidence about the failure, never a substitute for reading what the failure says.

Nor is a verdict read from silence. A review lives in the file the reviewer writes, so a reviewer that ended without writing it has not answered — its absence says nothing about the work, and least of all that the work failed.

## An unrecognized fault surfaces

The default has a direction, and the direction is outward. A fault the orchestrator cannot recognize is treated as a defect to be seen, not as weather to be waited out. Absorbing an unknown fault hides a defect behind a retry; surfacing a known-transient one costs a run. The asymmetry is deliberate: a visible defect is cheap to correct, an invisible one is not. Recognition is therefore something the orchestrator claims explicitly, never something it assumes.

## What each kind is owed

| Kind of fault | What the run does |
|---|---|
| A judgment about the work | The review cycle's own budget decides — see [outcomes.md](outcomes.md) |
| A transient infrastructure fault | Retried a bounded number of times |
| A fault outside an agent's authority | Escalation — see [escalation.md](../features/escalation.md) |
| An unrecognized fault | Surfaces |

## The known faults

Every fault the orchestrator can meet, and the handling it requires.

**From the agent runtime**

| Fault | What the run does |
|---|---|
| An overloaded API | Retried a bounded number of times |
| A transport failure that arrives with no answer at all | Retried a bounded number of times, then a halt |
| A transport failure that arrives carrying its own error text | Handled identically to a transport failure that arrives with no answer at all |
| An exhausted rate limit reported by the agent | A halt |
| A genuine invocation error — a bad argument, a missing file, an unusable session | Surfaces as a defect, not absorbed |
| An answer that is empty when one was required | Surfaces as a defect, not absorbed |
| A reviewer that ends without writing its review | A halt; the resumed run repeats that review |

**From the budget**

| Fault | What the run does |
|---|---|
| The session threshold is exceeded | A halt — see [usage-limits.md](../features/usage-limits.md) |
| The weekly threshold is exceeded | A halt — see [usage-limits.md](../features/usage-limits.md) |

**From the environment**

| Fault | What the run does |
|---|---|
| A notification that cannot be sent | Logged; the run continues |
| A push that fails | Logged; the run continues — see [target-project.md](../reference/target-project.md) |
| Usage output that cannot be read | Logged; the run continues without gating on it — see [usage-limits.md](../features/usage-limits.md) |
| A user-slug script that is missing or fails | A halt |

**From configuration or the operator**

| Fault | What the run does |
|---|---|
| No usable identity for a named roadmap | A halt |
| A named roadmap that does not exist | A halt |
| An owner line that does not match | A halt |
| Recognized invalid, corrupt, unreadable or ambiguous persisted resume metadata, or a resumable runtime-owner mismatch | A halt — see [Resume](../features/resume.md) |
| A resume that would start past the attempt budget | A halt |
| An operator stopping the run | A halt — a soft stop lets the current task finish, with the machine kept awake until it has, a forced one ends it immediately |

**Not faults at all**

| Not a fault | What it is |
|---|---|
| A review cycle that spends its budget without a pass | A judgment about the work — see [outcomes.md](outcomes.md) |
| An agent that reaches the boundary of its authority | Escalation — see [escalation.md](../features/escalation.md) |

## Reporting a fault

### Operator notifications

An outcome reaches a human through the notification, and it may not be ambiguous. Delivery is attempted, not guaranteed: a send is tried once, and one that fails is logged and nothing more — the fault that stops a run is routinely the fault that stops the report of it. Nothing is lost by that, because the notification was never the account. The account is in the run's own output and in the artifacts the run wrote, which is where a person goes when the alert never came. Invariant 5 of [outcomes.md](outcomes.md) holds throughout — a failed send never changes the outcome.

**A notification is a signal, not the account.** It says that something happened and to which project — never the account itself, and never where to find it. The full account lives in the artifact the run wrote and in the run's own output, which is where a person goes once the signal has reached them. A notification that carries the account instead stops being readable at a glance, which is the only thing a notification is for. The detail of an outcome the run composes itself is therefore short — a fixed clause, a task's name or a number, or nothing at all — and never a filesystem path or a line an agent wrote; an unforeseen error's alert carries the exception's type and the first line of its message.

**Its colour says what happened to the work.** A notification about the work says whether it advanced, failed to converge, or reached a decision that needs a person; a notification about the machine says none of that, because nothing happened to the work at all. The colour answers this before the text is read, separating what asks for a change in a roadmap or a spec from what asks for nothing of the kind.

| Colour | What it says |
|---|---|
| 🔴 | The work: it did not converge within its attempt budget. |
| 🔵 | The work: it reached a decision that is a person's to make. |
| 🟢 | The work: it advanced — a task closed and was committed, or every task in the roadmap is done. |
| 🟡 | The machine: nothing happened to the work at all; the machine got in the way, and no roadmap or spec needs touching. |

Three of the four speak about the work — red, blue, and green — and one, yellow, about the machine. Within the three, red and blue stay distinct: red is a verdict about the work, blue makes no verdict but still needs a roadmap or a spec changed. Which token in a configuration enables which alert lives in [configuration.md](../reference/configuration.md).

**One shape, composed in one place.** What a notification looks like is a property of the outcome it reports, never of the site that raised it. Every operator notification the orchestrator sends is composed in one place — the ones that report success no less than the ones that report a fault — so a new outcome cannot introduce a new shape by accident.

### Initiator completion

The [completion launcher](../features/completion-launcher.md#initiator-interface) has a separate peer-completion interface. Its queued content and delivery evidence follow that feature and its [reference](../reference/completion-launcher.md), rather than the operator text/colour format above. Delivery remains attempted, and failure to deliver does not change the underlying outcome.

## Invariants

1. **A transport failure is never a verdict.** However much output preceded it, a fault in the transport is evidence about the fault, not a judgment about the work.
2. **An unrecognized fault surfaces rather than being absorbed.** The orchestrator does not wait out a condition it cannot name.
3. **Recognition is explicit, never inferred from how far a call got.** How much output arrived before a failure describes the failure; it does not classify it.
4. **Delivery is attempted, never guaranteed, and never consequential.** A send that fails is logged and changes nothing, and the report itself is not lost — it is where the run wrote it.
5. **An operator notification is a signal, not the account.** Its detail is short and never a path or an agent's line, except an unforeseen error's, which carries the exception's type and first line.
6. **An operator notification's colour says whether it is about the work or the machine, and if the work, what happened to it.** Nothing else decides it.
