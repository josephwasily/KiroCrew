"""Liveness tests for the shell-command gate.

``is_sensitive_bash_command`` runs synchronously on the gateway's event loop,
under a loop-stall watchdog that hard-exits the process after 25 s of silence
(``dashboard.loop_stall_exit_after_secs``). Path-matching passes over the command
are quadratic, so a ~9 KB command full of ``https://`` URLs can stall the loop
past that budget. The gate does not match paths in command text at all -- and
what it still runs
(the size ceiling, the IMDS detector and the environment-credential detector) is
pinned here at the crash size, under the ceiling, where it is what the loop
actually pays. The ceiling itself is pinned as a refusal, not a skip: a command
too long to scan is denied rather than let through unscanned.
"""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

from kiro_crew import security
from kiro_crew.security import (
    MAX_SCANNABLE_COMMAND_CHARS,
    MAX_SCANNABLE_SOURCE_BODY_CHARS,
    is_sensitive_bash_command,
)

# ─────────────────────────────────────────────────────────────────────────────
# Shapes
# ─────────────────────────────────────────────────────────────────────────────

_SEG = "a" * 60


def _double_separator_command(n: int) -> str:
    """n path operands, each with a doubled ``//`` -- the shape that would send
    the command through a separator-collapsed re-scan."""
    return "ls " + " ".join(f"/opt//{_SEG}" for _ in range(n))


def _url_payload_command(n: int) -> str:
    """The field shape: a JSON body of ``https://`` URLs handed to curl."""
    urls = [f"https://tasks.example.test/T{100000 + i}?view=full&x={_SEG[:20]}" for i in range(n)]
    return "curl -s -X POST -d " + json.dumps({"items": urls})


# ─────────────────────────────────────────────────────────────────────────────
# Package shape: the split must not grow back into a monolith
# ─────────────────────────────────────────────────────────────────────────────


#: Ceiling on the whole security PACKAGE, not on any one file in it. The controls
#: were one module of about 21,800 lines, and the split adds a re-export block, an
#: export manifest and the mirroring facade on top of the code it relocates, so the
#: budget is that size plus room for the machinery, plus the redaction record,
#: credential-source and allowed-host modules, plus the resolver child script
#: (``_child_realpath.py``, ~190 lines) that lives beside the resolver it serves
#: rather than in the pool package. It is a bound on total volume:
#: relocating a declaration between submodules moves nothing across it.
#:
#: Raised again, from 27,200, when the facade stopped binding re-exported names
#: eagerly and began resolving each through its owner. That trades one import block
#: for two name lists -- an owner table and a ``TYPE_CHECKING`` block, one line per
#: exported name in each -- which measured 618 lines at the current surface and is
#: machinery, not control logic.
#:
#: Raised again, from 27,751, for the write-protected home entries covering the MCP
#: launch-approval directory and ``mcp/resolved``: gatewayd spawns an approved stub's
#: backend outside the sandbox, so a session must not be able to write either path.
#:
#: Raised again, from 27,761, for the ssh self-target refusal note: it says how long
#: a retry can still land inside the background check and names the IP-literal case
#: where this machine's address list cannot be read, so a refused agent knows when
#: to stop retrying and what to use instead.
#:
#: Raised again, from 27,766, for the write-protected home entry covering the kiro-cli
#: global MCP registry (``~/.kiro/settings/mcp.json``) and its ``KIRO_HOME``
#: re-anchoring: an ``autoApprove`` on an entry there is honoured by default and skips
#: the tool gate entirely, while the entry that decides it is admitted on its name
#: shape rather than on who wrote the file -- so an agent-writable registry grants its
#: own verbs a standing bypass. The reasoning for one leaf is most of the cost, which
#: is the shape every entry on this tier has.
#:
#: Raised again, from 27,814, for resolving the ``$HOME``-rooted form of both kiro-cli
#: write-tier leaves rather than only their ``KIRO_HOME`` copies. Anchoring them
#: lexically covered a symlinked ``$HOME`` itself but not one further down the path, so
#: a dotfile-managed ``~/.kiro`` left the real spec dir and the real MCP registry outside
#: the fence while their ``~``-spelled paths stayed inside it. The cost is the reasoning
#: plus one shared tuple, which is what replaces a second per-leaf arm.
#:
#: Lowered to 27,851 by SUBTRACTION. An earlier revision of this branch also emitted
#: every kiro-cli target in a second, all-forward-slash spelling, on the premise that a
#: Windows root could reach the anchor builder carrying the operator's own separators.
#: That premise is false: every root arrives through ``_resolve_root_anchors``, which
#: returns ``_realpath_or_none(expanded) or _lexical_root(expanded)``, and both answer
#: in the native spelling -- ``_lexical_root``'s ``os.path.normpath`` converts
#: ``C:/Users/x`` to ``C:\Users\x``. With no input that fails without it, the second
#: spelling was decoration, and on POSIX it would fence a bogus neighbour for any file
#: whose name contains a backslash. It is removed together with the two tests that
#: existed only for it; the ``$HOME``-symlink coverage passes without it, which is what
#: shows it was never load-bearing.
#:
#: Raised again, from 27,851, because the ``KIRO_HOME`` half was two hardcoded
#: per-leaf arms while the ``$HOME`` half looped the tuple -- so the tuple's own
#: comment ("a third leaf joins both halves by landing here") was false, and a third
#: leaf would have been fenced under ``$HOME`` and writable under the override. Both
#: halves now loop the same tuple, each leaf's tail is spelled once, and a test adds a
#: probe leaf and asserts BOTH spellings refuse -- it fails on the old code.
#:
#: Re-pinned again, on top of every raise above, for the ``panel-dismissals`` leaf
#: added to ``_CREW_SECRET_LEAVES`` in ``paths.py``: one entry plus the comment
#: stating why nothing a run can reach may forge or delete the operator's dismissal
#: records. Ten lines, all of them the fence declaration and its reason -- no new
#: control logic and no new matching pass. This branch's raise and the ones above it
#: are independent additions to the same ratchet, so the number below is re-MEASURED
#: off the tree rather than being the arithmetic sum of the deltas.
#: Raised again, from 27,863, for the own-address startup warm in ``argv_floor``:
#: the gateway starts the netlink read at boot, the worker reads and publishes that
#: table before any DNS lookup, and the publish merges the addresses and opens the
#: IP-literal window in one lock hold while each check reads the window flag before
#: the names, so the first ssh after a restart is not refused as this machine and a
#: secondary own IP is never admitted mid-publish. A dump that ends without
#: NLMSG_DONE, or that the kernel flags NLM_F_DUMP_INTR, counts as unread, so a
#: partial table never opens the window. So does an NLMSG_DONE whose errno is not 0.
#: Three incomplete dumps in a row log one warning, so a host whose table never
#: reads can be told apart from a target that is really this machine.
#:
#: Re-pinned from 27,942 for the NUL blanking in ``inline_payload._lex``: one line
#: that swaps each NUL for a space before tokenizing, plus the docstring saying why.
#: CPython 3.12 raises ``SystemError`` for a NUL after an indented block, which
#: escaped the lexer and crashed the gate on an ordinary ``b'\0'`` in a payload.
#: No new rule and no new matching pass.
#: Raised again, from 27,948, by one line: the ``vouched-executions`` entry in
#: ``_SENSITIVE_HOME_DIRS``. Each file there is the gateway's restart-surviving
#: word that a session may reach its member's private store, so no file tool may
#: write it. The fuller reason lives beside its ``sandbox._CREW_HIDDEN_LEAVES`` mask.
#:
#: Re-pinned from 27,949 for ``redaction._DOCUMENT_LINK_RE``: pass 3 skips a run
#: wholly inside a Google Docs, Drive or Confluence link of a fixed route, because a
#: document id is the same random base64 a key is and no gate can split the two.
#: One route regex, one span helper, a four-line check in pass 3, and the comment
#: naming the residual. No pass widened and no threshold moved.
#:
#: Raised again, from 28,025, for the ssh self-target floor's boot-time warm-up: the
#: own-address table is read at gateway startup and published before the DNS
#: lookups, and background threads parse the hosts-file table (in bounded chunks,
#: keyed on the own-address set it was judged by). On a miss the gate path parses
#: only a file that fits in one read chunk; a larger file is refused as pending
#: until the background parse is cached. On Windows, where ``st_ctime`` is creation
#: time, the key also carries a content digest (``hosts_file.py``, which holds the
#: line parser and digest helpers apart from ``argv_floor``'s per-module cap). A
#: Windows file too large to hash on the gate is never served, so a dotless target
#: there is pending, and the ssh self-target refusal note says so.
#:
#: Raised again, from 28,399, by two lines: case (1) of the ssh self-target refusal
#: note names a dotless target refused while a hosts file over 64 KiB is still read
#: in the background, so a caller retries it rather than treating it as settled.
#:
#: Raised again, from 28,401, for the containment gate's ``pre_resolved`` keyword:
#: one keyword on ``path_contains_sensitive``, forwarded to the two helpers that
#: already take it, plus the preconditions it carries written on the keyword
#: itself. The claim is ``is_sensitive_resolved_path``'s, unchanged: the caller
#: holds the canonical spelling and is off the event loop, so the anchors resolve
#: inline. No new entry point, no target, no matching rule and no threshold moved.
#:
#: The number IS the package's measured total, carrying no spare room: a ratchet with
#: headroom admits exactly the unreviewed growth it exists to catch, so the next line
#: added here fails this gate and has to be re-pinned deliberately, with its reason
#: written above. The guards that detect a monolith growing back are the per-file cap
#: and the facade's share below, and both must stay untouched.
#:
#: Raised for the ``registry_trust.json`` leaf added to ``_CREW_SECRET_LEAVES`` in
#: ``paths.py``: the operator's grants of ``owner`` trust to a hand-configured app
#: registry live in a keystone file on the same read+write floor as
#: ``denied_commands.json``, so the leaf and its two-line reason are three lines the gate
#: cannot avoid.
#:
#: Raised for the read-only bash gate's refusal of variable-assigning expansions
#: (`$[...]`, an `=` after `${`): one pattern alternative plus its reason comment.
#:
#: Raised for six stdout-only filters on the read-only bash allowlist (`tr`, `nl`,
#: `rev`, `comm`, `od`, `column`) and their reason comment.
#:
#: Raised for pass 3's macOS per-user directory exemption in ``redaction``:
#: withholding this host's own ``confstr`` id from the bare-secret scan, so a macOS
#: temp path (a computer-use screenshot among them) is not read as a key, costs the
#: id lookup, its grammar, the per-id pattern, the reason only the host's own id is
#: safe to withhold, and window classification with whole-run context that exempts
#: only windows sharing ≥ 24 bytes with that id while every other positive window
#: redacts each piece it touches. One mechanism, no new pass.
#:
#: Raised again, from 28,551, for the Windows alias fold in ``paths.py``: one lexical
#: helper strips a local-drive namespace prefix and a default-stream suffix, and
#: ``_candidate_forms`` resolves the folded spelling while keeping the raw one as a
#: candidate. No target, no matching rule and no threshold moved.
#:
#: Raised for the recursive-force ``rm`` deletion floor in the ``rm_floor.py`` sibling
#: module: an argv-structural gate that reads the ``rm`` command's own argv (flags in
#: any position/spelling, the ``$HOME``/``~``/glob targets, brace-expanded flags and
#: operands). It is a UNION with the two catalog regexes, which stay in the ``re`` tier
#: as a fail-closed deny-net, and the ``grep`` inert-search carve-out
#: (``_DENY_EXCEPTIONS``) narrows the regex's one false positive. A per-argv ``rm``-span
#: cap (``_RM_CLASSIFY_SPAN_CAP``) and the mover-operand skip keep the synchronous gate
#: linear on a hostile input. The floor delegates substitution/backtick/xargs/brace
#: scanning to the shared ``shell_normalizer`` / ``argv_floor`` helpers rather than
#: carrying private copies.
#:
#: Raised once more for the GPT 6.1 review-round fixes to this floor: xargs
#: ``--replace``/``--replace=VALUE`` replace-string detection, a projected-byte +
#: count overflow that fails CLOSED to the deny sentinel (never an appended argv),
#: and preserving a reconstructed-argv home verdict through the final quoted-home
#: suppression. All three are added security logic plus their reason comments.
#:
#: Raised again for two more review-round fixes: re-quoting a dequoted xargs
#: operand on serialization so a quoted ``;``/``|`` literal is not read back as a
#: command boundary that hides the home/root target, and bounding a ``printf``
#: producer's projected stdout (fail-closed deny on overflow) so a reused format x
#: many args cannot exceed the synchronous gate's watchdog.
#:
#: Raised once more for a cumulative brace-materialization byte budget
#: (``_RM_EXPANSION_BYTE_BUDGET``): the per-word count cap and per-span
#: classification cap do not bound the BYTES a word's members carry, so a word at
#: the count cap with large members, re-materialized once per span, stalled the
#: synchronous gate past its watchdog; once the budget is exhausted a span falls
#: back to the cheap root/home-shape verdict instead of materializing members.
#:
#: LOWERED when the xargs unverifiable-expansion apparatus was subtracted: a blanket
#: deny of every ``sh -c`` / ``bash -c`` / ``eval`` xargs pipeline, the ungated
#: unverifiable-expansion floor, the NUL marker, the projected-byte caps and the
#: ``-I`` line-record splitter sat outside this change's Goal (rm flag spellings +
#: home variable spellings) and newly refused legitimate commands, so they were
#: removed. The rm-floor's own-argv home/root classification is unchanged.
#:
#: Adjusted when the rm-floor xargs RECONSTRUCTION moved out of ``shell_normalizer``
#: into ``rm_floor.py``: the general payload scan now uses ``shell_normalizer``'s
#: base string ``_xargs_reconstructed_command`` (byte-identical to main), while the
#: flag-keeping reconstruction the rm floor needs to see a home/root wipe through an
#: xargs pipeline lives beside the floor that consumes it. A replace-count overflow
#: on that rm-floor path is treated as unverifiable and denied with a reason (rm
#: floor only). The lines moved between files; the package total is near flat.
#:
#: Raised for the minimal round-12 producer-output byte cap restored to
#: ``rm_floor.py``: a ``printf`` producer reuses its format per argument batch, so a
#: long format x many args projects to tens of MB that stalled the gate; the
#: projection is bounded before the string is built and an overflow goes through the
#: same unverifiable denial path (rm floor only).
#:
#: Raised for two more rm-floor fixes: a CUMULATIVE projected-byte cap in the
#: replacement substitution (one operand with thousands of recurring ``{}`` x many
#: records projects to tens of MB -> the overflow sentinel), and running the
#: executed shell ``-c`` payload pass in the heavy-substitution fallback so a
#: ``bash -c`` / ``sh -c`` home wipe buried behind many substitution openers is
#: classified instead of skipped.
#:
#: Raised for the budget-fallback xargs reconstruction + the per-span operand cap:
#: both the heavy-substitution and budget-exhausted fallbacks now run the bounded
#: xargs reconstruction (an ``echo ~ | xargs rm -fr`` wipe hidden behind openers no
#: longer slips), and a single ``rm`` span past ``_RM_SPAN_OPERAND_CAP`` operands
#: falls back to the cheap root/home-shape verdict so a bare-``rm`` flood classifies
#: in bounded work (the cap cuts before the O(operands) structural scans).
#:
#: Raised for the single quote-aware boundary helper (``_rm_token_ends_argv``, every
#: rm-floor scan/fallback routes through it so a quoted ``'a;b'`` is data, not a
#: span end), the expanded-home equality in the overflow-span scan, and the shared
#: xargs-reconstruction + flat-token budgets that bound the frame walk to O(1) total
#: past the frame budget (a 200-pipeline line classifies in bounded work).
#:
#: Raised for three more rm-floor fixes: the data-consumer ``-c``-payload scan
#: precomputes command boundaries so a long ``echo sh … rm`` mention is O(n) not
#: O(n x n); ``xargs -d`` / ``--delimiter`` is honored when splitting stdin records
#: (``printf '%s:' ~ | xargs -d: rm -fr`` is a home wipe); and the operand cap
#: counts RAW operands (not brace-expanded members) with an empty-alternative suffix
#: beside a range group ruled non-catastrophic, so a numbered brace cleanup
#: (``rm -rf {1..70}{,.log}``) classifies as the descendant cleanup it is.
#:
#: Raised again: the xargs reconstruction budget switched from a COUNT cap (which
#: discarded, then over-refused, the unclassified remainder) to a cumulative-BYTES
#: budget with per-source memoization, so every pipeline whose bytes fit is
#: classified -- a home wipe past the 64th pipeline denies while a benign many-
#: pipeline line stays allowed (GPT 6.1 F1, security-class) -- and the data-consumer
#: scan's O(n) bound is proved by a deterministic operation-count assertion instead
#: of a wall-clock bound (GPT 6.1 F2, tests-are-deterministic).
#:
#: LOWERED when the ENTIRE rm-floor xargs/printf/replacement RECONSTRUCTION apparatus
#: was subtracted (maintainer ruling: the Goal is rm flag spellings + home variable
#: spellings; xargs emulation is not the Goal and kept producing security findings in
#: its own emulation). Removed from ``rm_floor.py``: the flag-keeping reconstruction
#: and its pipeline walk, the ``echo``/``printf`` producer-output resolver, the
#: ``-I``/``-i``/``--replace`` replacement expansion, the ``-d`` delimiter decode,
#: the record-split/quote helpers, the cumulative byte budgets and the unverifiable
#: overflow sentinel; and from ``__init__.py`` the dead unverifiable deny branch. A
#: home/root wipe reached THROUGH an xargs pipeline is left to main's general scan +
#: whole-text deny-net regex, exactly as main does. The rm-floor's own-argv home/
#: root flag/spelling classification, the quote-aware boundary helper, the frame/
#: token budgets and the executed ``bash -c`` payload classification are unchanged.
#:
#: Raised slightly for the heredoc quoted-delimiter capture fix: the opener regex
#: now captures a QUOTED delimiter with interior spaces (``<<'END OF TEXT'``) so the
#: body-strip ends at the real terminator instead of truncating to ``END`` and
#: dropping a trailing ``rm -fr ~`` (GPT 6.1 security-class home-wipe bypass).
#:
#: Raised again for the heredoc CROSS-LINE quote-state fix: the body-stripper now
#: computes quote / command-substitution masks over the WHOLE source once (indexed
#: by absolute offset) and folds backslash line-continuations first, so a ``<<EOF``
#: inside a multi-line quoted string is seen as quoted and opens no heredoc -- the
#: per-line masks had reset state each line, taking it as an unterminated heredoc
#: and discarding a trailing ``rm -fr "$HOME"`` (GPT 6.1 security-class bypass). The
#: stale ``xargs stdin`` protection claim in ``denied_rules.py`` was also removed.
#:
#: Raised for the token-budget-exhaustion fix: past the frame-descent cap, every
#: remaining frame now also gets a CHEAP, bounded, quote-normalized exact root/home
#: scan (``_rm_frame_overflow_targets``) instead of being silently skipped once the
#: shared 4096-token budget is spent -- a padded line of many substitutions
#: exhausted the budget before an obfuscated ``r''m -fr ~`` frame and the split
#: spelling slipped the whole-text regex (GPT 6.1 security-class, UNBOUNDED). The
#: expensive full classification stays budget-gated, so total work stays bounded.
#:
#: Net change this round: all reviewer/round citations were stripped from
#: ``rm_floor.py`` comments (present-tense behavioral descriptions only), and three
#: fixes were added -- a bounded ONE-LEVEL descent into the trailing unbalanced
#: ``$(`` so a wipe hidden after a long opener run is caught on the heavy linear
#: path (the opener cap is lowered to 48, which stays fail-closed because of this
#: descent, closing a ~24 s frame-walk stall past the 25 s watchdog); an unterminated
#: UNQUOTED heredoc opener now keeps the remaining lines (a ``<<`` bash never reads
#: as a heredoc keeps a trailing wipe for classification); and the heavy innermost-body
#: scan also runs the de-quoted exact scan so a split spelling is caught there too.
#:
#: LOWERED when ``shell_normalizer.py`` was reverted to byte-identical with main
#: (all four PR hardenings removed, per the invariant that this module stays
#: unchanged): the quote-reset in ``_matching_close_paren`` had regressed the
#: git-publish floor on a quoted ``"$(command -v git)"`` program word, and the home
#: var-boundary / peel-cap belong in the floor, not the shared module. The behaviors
#: still needed are re-homed in ``rm_floor.py``: a ``$HOME``-prefixed DIFFERENT
#: variable (``$HOME_BAK``) stays unresolved rather than collapsing to root via
#: ``..``; ``flock FILE -c 'cmd'`` is classified as an executed shell payload; and a
#: run of child-glob stars (``~/**`` / ``/***``) is treated as the target itself.
#:
#: Raised for two more rm-floor fixes: the span scan uses a QUOTE-AWARE substitution
#: depth delta (``_rm_substitution_depth_delta``) so a literal ``)`` inside a quoted
#: operand (``rm -fr 'a)b' ~``) does not end the argv before a later target; and the
#: heavy-path top-level segment loop also runs the de-quoted exact scan, so a split
#: spelling (``r''m``) after a run of substitutions past the opener cap is caught
#: there as it already is for substitution bodies and the unbalanced-opener tail.
#:
#: Raised for two more rm-floor fixes: a ``<<`` under an unclosed ``${`` parameter
#: expansion (``echo ${v:-<<'x'}``) opens no heredoc, so the stripper does not drop
#: a real trailing ``rm -rf "$HOME"``; and a command-string wrapper (``watch 'rm -rf
#: "$HOME"'``) surfaces its first non-flag operand as an executed shell payload. An
#: ``identity-home-path`` scan literal in a comment was also replaced with a
#: placeholder.
#: Raised for the single quote-aware boundary consolidation: the DECODED-view
#: classification kept operands de-quoted and ended an ``rm`` span at a non-quote-
#: aware ``_ends_argv``, so a split spelling whose operand is a literal filename
#: holding ``;``/``|`` (``r''m -fr 'a;b' ~`` / ``r''m -fr "a;b" $HOME``) lost the
#: trailing home target. The decoded view now decodes only the program and flag
#: WORDS (operands kept literal via ``_rm_decoded_argv_preserving_operands``) and
#: every boundary test routes through the one quote-aware ``_rm_token_ends_argv``;
#: the non-live-home suppression masks per TOKEN so a double-quoted operand does not
#: re-expose its separator. ``_rm_frame_overflow_targets`` also takes the quote-
#: preserving source at every call site.
#: Raised for one more rm-floor fix: a redirection ``&`` (``2>&1``, ``1>&2``,
#: ``&>/dev/null``, ``>&2``) duplicates a file descriptor and does not end the
#: command, but the boundary scan split on it as a backgrounding ``&``, so
#: ``rm -fr 2>&1 ~`` lost the trailing home operand. The two boundary helpers were
#: MERGED into the single ``_rm_unescaped_boundary`` (the former
#: ``_rm_boundary_outside_quotes`` is gone; its one caller passes
#: ``treat_subshell_closer=False``), and that one scan exempts a redirection ``&``
#: via ``_rm_amp_is_redirection``; a real backgrounding ``&`` and the ``&&`` operator
#: still end the command.
_PACKAGE_LINE_BUDGET = 31_433

#: Ceiling on any ONE file in the package. This is what the bound is really for --
#: a package total says nothing about a single file growing back into a second
#: monolith, and a per-file cap is what a whole-file bound on the pre-split module
#: could not express. Set with headroom over the largest cluster so ordinary growth
#: does not trip it; a cluster that reaches it is asking to be split, and RAISING
#: the number is not the fix.
_MODULE_LINE_CAP = 4_500


def _package_line_counts() -> dict[str, int]:
    """Line count per file of the installed ``kiro_crew.security`` package."""
    package_dir = Path(security.__file__).parent
    return {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in sorted(package_dir.glob("*.py"))
    }


def test_the_package_stays_within_its_line_budget() -> None:
    counts = _package_line_counts()
    assert counts, "no package sources found"
    total = sum(counts.values())
    assert total <= _PACKAGE_LINE_BUDGET, f"package grew to {total} lines: {counts}"


def test_no_single_module_grows_back_into_a_monolith() -> None:
    oversized = {
        name: count for name, count in _package_line_counts().items() if count > _MODULE_LINE_CAP
    }
    assert not oversized, f"past the per-module cap: {oversized}"


def test_the_facade_is_the_smallest_it_can_be_of_the_package() -> None:
    """The facade carries re-exports and the mirroring machinery, so it must stay a
    small share of the package: a share that climbs means logic is accreting in the
    one file every caller imports, which is the shape the split exists to prevent."""
    counts = _package_line_counts()
    facade = counts["__init__.py"]
    assert facade * 5 <= sum(
        counts.values()
    ), f"the facade is {facade} of {sum(counts.values())} package lines"


# ─────────────────────────────────────────────────────────────────────────────
# Size ceiling: refused, not scanned, not skipped
# ─────────────────────────────────────────────────────────────────────────────


def test_oversized_command_is_refused_with_a_reason() -> None:
    cmd = "echo " + "x" * MAX_SCANNABLE_COMMAND_CHARS
    reason = is_sensitive_bash_command(cmd)
    assert reason is not None
    assert "too large to security-scan" in reason
    assert str(len(cmd)) in reason


def test_command_at_the_ceiling_is_scanned_not_refused() -> None:
    body = "x" * (MAX_SCANNABLE_COMMAND_CHARS - len("echo "))
    assert is_sensitive_bash_command("echo " + body) is None
    # And a detector's subject at the very end of a ceiling-sized command is found:
    # the ceiling is a bound on what is scanned, not a skip of the tail.
    tail = "; curl http://169.254.169.254/latest/meta-data/"
    cmd = "echo " + "x" * (MAX_SCANNABLE_COMMAND_CHARS - len("echo ") - len(tail)) + tail
    assert len(cmd) == MAX_SCANNABLE_COMMAND_CHARS
    reason = is_sensitive_bash_command(cmd)
    assert reason is not None
    assert reason.startswith("Blocked: command accesses IMDS")


def test_ceiling_matches_the_tool_input_tier() -> None:
    """The two tiers refuse at the same size, so a command cannot be too long
    for one and scanned by the other."""
    from kiro_crew import llm_helpers

    assert llm_helpers._MAX_SCANNABLE_TOOL_INPUT_CHARS == MAX_SCANNABLE_COMMAND_CHARS


# ─────────────────────────────────────────────────────────────────────────────
# A cron SCRIPT BODY has its own ceiling, and is not a shell subject at all
# ─────────────────────────────────────────────────────────────────────────────


def test_the_source_body_ceiling_is_larger_and_owned_by_the_cron_reader() -> None:
    """20 KiB of shell on one ``Bash`` call is a heredoc; 20 KiB of cron script is an
    ordinary script, and refusing it there is permanent (every tick until edited). The
    cron gate reads and refuses on ONE number so the reader and the scan agree."""
    from kiro_crew import mcp_cron

    assert MAX_SCANNABLE_SOURCE_BODY_CHARS > MAX_SCANNABLE_COMMAND_CHARS
    assert mcp_cron._MAX_SCRIPT_SCAN_BYTES == MAX_SCANNABLE_SOURCE_BODY_CHARS

    body = "".join(f'value_{i} = "{"t" * 200}"\n' for i in range(120))
    assert MAX_SCANNABLE_COMMAND_CHARS < len(body) <= MAX_SCANNABLE_SOURCE_BODY_CHARS
    assert mcp_cron._vet_script_contents(body) is None
    assert mcp_cron._vet_script_contents(body + 'open("~/.aws/credentials")\n') is not None

    over = "x = 1\n" * MAX_SCANNABLE_SOURCE_BODY_CHARS
    reason = mcp_cron._vet_script_contents(over)
    assert reason is not None and "too large to security-scan" in reason


def test_the_shell_gate_has_no_source_body_entry_point() -> None:
    """RATCHET: ``is_sensitive_bash_command`` takes a shell command line and nothing
    else -- no subject flag, no re-pointed traversal subjects, no per-caller ceiling.
    Every one of those knobs existed once to make a Python source body survive a
    shell-grammar pass, and each pass still produced a false-denial class on ordinary
    scripts. A source body is not this gate's subject; see
    ``mcp_cron._vet_script_contents``."""
    params = inspect.signature(security.is_sensitive_bash_command).parameters
    assert set(params) == {"command", "enabled_ids"}, sorted(params)
    for name in (
        "is_sensitive_source_body",
        "_source_command_subjects",
        "_sensitive_run_in_source_literals",
        "_parse_source_body",
        "_SOURCE_PATTERN_SINKS",
        "_SOURCE_COMMAND_SUBJECT_CAP",
    ):
        assert not hasattr(security, name), name


# ─────────────────────────────────────────────────────────────────────────────
# Liveness at the crash size, under the ceiling
# ─────────────────────────────────────────────────────────────────────────────


def _gate_seconds(command: str) -> float:
    started = time.perf_counter()
    is_sensitive_bash_command(command)
    return time.perf_counter() - started


def test_double_separator_10kb_is_fast() -> None:
    """The crash shape, at the crash size: 15 s on the shipped build."""
    cmd = _double_separator_command(160)
    assert 10_000 < len(cmd) <= MAX_SCANNABLE_COMMAND_CHARS
    assert is_sensitive_bash_command(cmd) is None
    assert _gate_seconds(cmd) < 2.0


def test_url_payload_12kb_is_fast() -> None:
    cmd = _url_payload_command(160)
    assert 10_000 < len(cmd) <= MAX_SCANNABLE_COMMAND_CHARS
    assert is_sensitive_bash_command(cmd) is None
    assert _gate_seconds(cmd) < 2.0
