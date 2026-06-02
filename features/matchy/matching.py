"""Weekly Matchy pairing: plan 2/3-person groups, respect history, avoid duplicates."""
import random
from dataclasses import dataclass

from features.matchy.store import load_roster, save_roster


@dataclass
class ValidationResult:
    ok: bool
    message: str = ""


def unique_ids(ids: list[str]) -> list[str]:
    seen = set()
    out = []
    for uid in ids:
        if uid and uid not in seen:
            seen.add(uid)
            out.append(uid)
    return out


def can_pair(a: str, b: str, history: dict) -> bool:
    prev_a = history.get(a) or []
    prev_b = history.get(b) or []
    return b not in prev_a and a not in prev_b


def combinations_mostly_exhausted(members: list[str], history: dict) -> bool:
    n = len(members)
    if n < 2:
        return False
    max_pairs = n * (n - 1) / 2
    met = set()
    member_set = set(members)
    for uid in members:
        for other in history.get(uid) or []:
            if other in member_set:
                key = (uid, other) if uid < other else (other, uid)
                met.add(key)
    return len(met) >= max_pairs * 0.8


def plan_group_sizes(n: int) -> list[int]:
    """Balanced mix of 2- and 3-person groups covering all n people."""
    if n <= 0:
        return []
    if n == 1:
        return [1]

    best: list[int] | None = None
    best_gap = n + 1

    for threes in range(n // 3, -1, -1):
        rem = n - 3 * threes
        if rem < 0 or rem % 2:
            continue
        twos = rem // 2
        sizes = [3] * threes + [2] * twos
        if not sizes:
            continue
        gap = abs(threes - twos)
        if gap < best_gap:
            best_gap = gap
            best = sizes

    return best or ([2] * (n // 2) + ([1] if n % 2 else []))


def sanitize_groups(groups: list[list[str]]) -> list[list[str]]:
    """One Slack user per group; each user in at most one group."""
    cleaned = []
    used = set()
    for group in groups:
        row = unique_ids(group)
        row = [uid for uid in row if uid not in used]
        for uid in row:
            used.add(uid)
        if row:
            cleaned.append(row)
    return cleaned


def merge_stragglers(
    groups: list[list[str]], *, max_size: int = 3
) -> tuple[list[list[str]], list[str]]:
    """Place 1-person groups into existing teams; repeats allowed for stragglers."""
    teams = [g for g in groups if len(g) >= 2]
    waiting = [g[0] for g in groups if len(g) == 1 and g[0]]
    unmatched = []

    for uid in waiting:
        placed = False
        for team in sorted(teams, key=len):
            if uid in team or len(team) >= max_size:
                continue
            team.append(uid)
            placed = True
            break
        if not placed:
            unmatched.append(uid)

    while len(unmatched) >= 2:
        teams.append([unmatched.pop(0), unmatched.pop(0)])

    if len(unmatched) == 1 and teams:
        uid = unmatched.pop()
        for team in sorted(teams, key=len):
            if uid not in team:
                team.append(uid)
                uid = None
                break
        if uid:
            unmatched.append(uid)

    return sanitize_groups(teams), unmatched


def finalize_groups(
    groups: list[list[str]], history: dict, *, allow_repeats: bool
) -> tuple[list[list[str]], list[str]]:
    """Pair leftover solos when possible, then force-merge any stragglers."""
    teams = [list(g) for g in groups if len(g) >= 2]
    solos = [g[0] for g in groups if len(g) == 1 and g[0]]

    i = 0
    while i < len(solos):
        a = solos[i]
        paired = False
        for j in range(i + 1, len(solos)):
            b = solos[j]
            if a != b and (allow_repeats or can_pair(a, b, history)):
                teams.append([a, b])
                solos.pop(j)
                solos.pop(i)
                paired = True
                break
        if not paired:
            i += 1

    teams.extend([[uid] for uid in solos])
    return merge_stragglers(sanitize_groups(teams))


def validate_groups(groups: list[list[str]], roster: list[dict]) -> ValidationResult:
    names = {m["slackId"]: m.get("name") or m["slackId"] for m in roster if m.get("slackId")}
    seen_global: dict[str, list[int]] = {}

    for i, group in enumerate(groups):
        seen_local = set()
        for uid in group:
            if not uid:
                continue
            if uid in seen_local:
                label = names.get(uid, uid)
                return ValidationResult(
                    ok=False,
                    message=(
                        f"❌ *Matchy generation aborted:* duplicate user in a single group.\n\n"
                        f"• *{label}* (`{uid}`) appears twice in group {i + 1}\n\n"
                        "_No group chats were created. Previous matches were not updated._"
                    ),
                )
            seen_local.add(uid)
            seen_global.setdefault(uid, []).append(i)

    dupes = [(uid, idxs) for uid, idxs in seen_global.items() if len(idxs) > 1]
    if dupes:
        lines = [
            f"• *{names.get(uid, uid)}* (`{uid}`): groups {', '.join(str(n + 1) for n in idxs)}"
            for uid, idxs in dupes
        ]
        return ValidationResult(
            ok=False,
            message=(
                f"❌ *Matchy generation aborted:* {len(dupes)} user(s) in more than one group:\n\n"
                + "\n".join(lines)
                + "\n\n_No group chats were created. Previous matches were not updated._"
            ),
        )
    return ValidationResult(ok=True)


def _grow_group(
    pool: list[str],
    used: set[str],
    target: int,
    history: dict,
    allow_repeats: bool,
) -> list[str]:
    starter = next((u for u in pool if u not in used), None)
    if not starter:
        return []

    group = [starter]
    used.add(starter)
    attempts = 0
    limit = max(80, target * 30)

    while len(group) < target and attempts < limit:
        candidates = [u for u in pool if u not in used]
        if not candidates:
            break
        pick = random.choice(candidates)
        if pick in group:
            attempts += 1
            continue
        if allow_repeats or all(can_pair(m, pick, history) for m in group):
            group.append(pick)
            used.add(pick)
        attempts += 1

    return unique_ids(group)


def _tuck_singleton(groups: list[list[str]], uid: str) -> list[list[str]]:
    if not uid:
        return groups
    for team in reversed(groups):
        if uid not in team and len(team) < 3:
            team.append(uid)
            return groups
    groups.append([uid])
    return groups


def assign_groups(member_ids: list[str], history: dict) -> list[list[str]]:
    """Build weekly groups for enabled members."""
    members = unique_ids(member_ids)
    if len(members) < 2:
        return []

    allow_repeats = combinations_mostly_exhausted(members, history)
    pool = members[:]
    random.shuffle(pool)
    used: set[str] = set()
    raw: list[list[str]] = []

    sizes = plan_group_sizes(len(members))[:]
    random.shuffle(sizes)

    for size in sizes:
        group = _grow_group(pool, used, size, history, allow_repeats)
        if len(group) >= 2:
            raw.append(group)
        elif len(group) == 1:
            raw = _tuck_singleton(raw, group[0])

    while True:
        leftover = next((u for u in pool if u not in used), None)
        if not leftover:
            break
        used.add(leftover)
        raw = _tuck_singleton(raw, leftover)

    teams, _ = finalize_groups(raw, history, allow_repeats=allow_repeats)
    return teams


def record_match_round(groups: list[list[str]]) -> None:
    """Persist pairwise history and bump matchyCount for placed members."""
    data = load_roster()
    history = data.setdefault("previousMatches", {})
    members = data.get("members") or []
    by_id = {m["slackId"]: m for m in members if m.get("slackId")}

    for group in groups:
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                history.setdefault(a, [])
                history.setdefault(b, [])
                if b not in history[a]:
                    history[a].append(b)
                if a not in history[b]:
                    history[b].append(a)
            member = by_id.get(a)
            if member:
                base = member.get("matchyCount")
                if base is None:
                    base = member.get("rep") or 0
                member["matchyCount"] = int(base) + 1

    save_roster(data)


def validate_match_assignments(groups: list, members_data: dict) -> dict:
    result = validate_groups(groups, members_data.get("members") or [])
    return {"ok": result.ok, "message": result.message}


def ensure_no_single_groups(
    matches: list, previous_matches: dict, allow_repeats: bool
) -> list[list[str]]:
    teams, _ = finalize_groups(matches, previous_matches, allow_repeats=allow_repeats)
    return teams


# Back-compat alias for tests
sanitize_match_groups = sanitize_groups
