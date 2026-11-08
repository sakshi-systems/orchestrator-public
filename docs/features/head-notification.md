# A head notifies the person

## What it is

A head that stops on a blocker in its own tasks tells the person through a command it runs.

The command needs no always-running process and no run: the head runs it from its own chat, at the moment it stops.

## The channel

The command sends through the orchestrator's operator notifications, the channel that already carries a run's outcome, a failure included, to the person. It therefore obeys [Operator notifications](../concepts/fault-handling.md#operator-notifications): the notification is a signal, never the account, so it says that a head stopped and for which project, and the person reads the head's chat for the reason; and its shape is composed in the one place where every operator notification is composed.

The command, the notification's content, its colour and its event type are in the [reference](../reference/head-notification.md).
