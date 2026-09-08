# Explorer agent system

`explorer_system` is the copy boundary for the Explorer research system. It is
a leaf Python package: it does not import Franta, does not publish into a host's
authoritative memory, and does not require a host's prompt dispatcher.

The block owns:

- Explorer worker prompts, model routing, and response schemas;
- scratch, direction, summary, and computation-evidence contracts;
- the append-only Explorer repository and frozen-turn snapshots;
- lineage/attempt state transitions and the fixed worker-wave program;
- joint-search machinery expressed through read-only ports; and
- the three Explorer skill documents shipped as package data.

A collaborator supplies one adapter implementing the ports in `interfaces.py`.
The adapter is responsible for its own process launcher, durable call authority,
read-only published-memory view, trusted-receipt transport, and handoff into its
own verification/integration workflow. Explorer never receives a published
memory write capability.

The adapter has six explicit jobs:

1. Construct `ExplorerRepository` with the collaborator's published-record,
   context, and output-ID patterns and its own `export_kinds` vocabulary, then
   construct `ExplorerService`. Explorer has no built-in host export kinds.
2. Implement `ExplorerHost` using the collaborator's durable call/CAS state,
   launcher, and targeted cancellation. Implement `ExplorerCollaborator` for
   the frozen-turn handoff, then let the outer loop call only
   `ExplorerProgram.advance_turn`. The fixed program retains lineage admission,
   three-attempt planning, deadlines, peer containment, graceful drain, freeze,
   and handoff acceptance.
3. Bind `AuditedExplorerAPI` to a read-only published-memory backend and a
   launch-scoped access policy. No published-memory write port exists.
4. Register the definitions and normalizers in `tools.py`, authenticating
   staged results before passing them to `ExplorerService.prepare_staged_result`
   and `trust_receipt`.
5. Materialize the packaged `skills/` assets, build Explorer worker calls through
   `agents.build_launch_spec`, and build sorter calls only through
   `main_sort.DEFAULT_MAIN_SORT_BLOCK`. Collaborator-specific sorter tool names
   are a `main_sort.HostSortTools` value supplied by the adapter. The same block
   interface owns the sorter response schema, frozen snapshot rendering and
   digest, allowed proposal vocabulary, provenance validation, and final result
   validation. The legacy `agents` main-sort exports remain compatibility
   delegates. For each new
   staged-access worker call, use `agents.select_guidance_variant` with durable attempt entropy,
   persist the returned enum in the exact call input, and pass it back as
   `guidance_variant` on every retry or recovery launch. Prompt rendering itself
   never selects a variant.
6. Implement `ExplorerCollaborator.accept_explorer_handoff` for the immutable
   `ExplorerHandoff` returned after drain. Verification, export, and the
   collaborator's own research turn remain outside Explorer.

For Franta, that integration is confined to `franta/explorer_adapter.py` plus
small compatibility re-exports for projects created before this extraction.
Copying this directory to a different collaborator therefore requires changing
the collaborator adapter, not Explorer prompts, repository, or control code.
