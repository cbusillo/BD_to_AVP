# Queue Workspace Mockup

This standalone SwiftUI app is the design evidence for issue #501. It explores a persistent, first-class conversion queue before the production worker and storage layers are changed.

The mockup intentionally uses fixture data. Its interactions demonstrate queue selection, ordering, removal, retry, start, scheduling, pause-after-current, stop-current, restoration, failures, and completed-work cleanup; they do not invoke the real conversion pipeline.

## Run

```sh
design/queue-mockup/build.sh
open "design/queue-mockup/build/BD to AVP Queue Mockup.app"
```

Use the toolbar to switch scenarios, appearance, and row density. The included scenarios cover empty, idle, running, paused, scheduled, restored, mixed-result, and dense queues.

## Capture

```sh
design/queue-mockup/capture.sh running light
design/queue-mockup/capture.sh running light /tmp/queue-minimum.png 920 760
```

Committed screenshots in `screenshots/` are the frozen visual reference for implementation. The production work should preserve the hierarchy and control semantics while replacing fixture state with the durable queue model described by #501 and its implementation subissues.
