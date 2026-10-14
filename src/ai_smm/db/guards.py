"""Database-enforced business-mutation guards (SMM-025D, FU-025C-1).

PostgreSQL itself refuses a business write from a Control Center role
unless it is made by an authenticated project owner, within the rules the
application follows -- whatever the application does. Design:
docs/adr/ADR-025D-database-enforced-mutation-safety.md.

Who is guarded. A trigger checks whether the role running the statement
(current_user) is a member of the role that owns the table. The owner --
the worker, migrations, the operator's CLI -- and superusers are trusted
and pass untouched; so do statements inside the SECURITY DEFINER
functions defined here and in smm_auth (they run as the owner). Every
other role is guarded: today ai_smm_cc (read-only) and ai_smm_cc_rw
(business mutations), and any role created later, without anyone having
to list it.

Who the caller is. A guarded transaction must first be bound to a live
session: guard_bind_session(raw session token) hashes the token, checks
the session exactly as smm_auth.session_resolve does (not revoked, not
expired, not idle, active user, plausible timestamps) and records
(transaction id -> user, session) in mutation_contexts, a table no
guarded role can write. Every guard reads the user from there; nothing a
caller passes names it. A leaked database credential without a session
token can therefore write nothing, and the role cannot mint a session
(smm-auth, ADR-024C-3A).

What is enforced, per table, for guarded roles:

* every business write: a bound context, and the bound user is an
  OWNER of the row's project; actor and creator columns equal the bound
  user;
* content_approvals: only the current revision; an approval consumes a
  fresh step-up grant (approval_grants), which only smm-auth issues after
  re-checking the user's password -- so a compromised API process cannot
  approve, even with the owner's cookie;
* publications: a new row is an approved, unscheduled, unclaimed snapshot,
  and at COMMIT it must carry valid lineage (link -> approved revision ->
  identical content and destination); updates are the three queue
  commands only, never a claim, and scheduling re-checks the lineage and
  the destination account;
* content_publication_links: only for a publication created in the same
  transaction, from the item's approved revision;
* social_accounts: a bound Threads identity never changes;
* social_account_credentials: only active -> superseded | revoked;
* projects, memberships, settings: a membership only as the owner of a
  project created in the same transaction;
* audit_log: written_by is always session_user; a business role writes
  business actions only, as the bound user; a role without business
  rights writes auth.* only (the read-only API before smm-auth).

The SQL is versioned. A migration imports the version it created and
never a later one; models.py builds the current version for
Base.metadata.create_all, and tests/test_db_guards.py checks that both
produce the same function bodies.
"""
from __future__ import annotations

from dataclasses import dataclass


#: ERRCODE of every guard refusal: 42501, the same SQLSTATE PostgreSQL
#: uses for a missing privilege.
GUARD_ERRCODE = "insufficient_privilege"

#: How long a step-up approval grant stays usable.
APPROVAL_GRANT_LIFETIME = "5 minutes"

#: Audit actions a business role may write (as the bound user).
BUSINESS_AUDIT_ACTIONS = (
    "content.%",
    "project.%",
    "social_account.%",
)
BUSINESS_AUDIT_EXACT = ("rescheduled", "cancelled")

#: Inline in every trigger: is the statement's role outside the table
#: owner? No helper call, so the read-only role needs no EXECUTE grant.
_GUARDED = (
    "NOT pg_catalog.pg_has_role(current_user, "
    "(SELECT c.relowner FROM pg_catalog.pg_class c WHERE c.oid = TG_RELID), "
    "'MEMBER')"
)

_HEADER = (
    "LANGUAGE plpgsql\n"
    "SET search_path = pg_catalog, pg_temp\n"
)


@dataclass(frozen=True)
class GuardFunction:
    name: str
    signature: str  # argument types, for GRANT / DROP
    ddl: str
    #: Roles that need EXECUTE (granted by the grants files, never here).
    callable_by_business_role: bool = False


@dataclass(frozen=True)
class GuardTrigger:
    name: str
    table: str
    ddl: str


def _fn(name: str, args: str, returns: str, body: str, *, extra: str = "") -> str:
    return (
        f"CREATE OR REPLACE FUNCTION public.{name}({args}) RETURNS {returns}\n"
        f"{_HEADER}{extra}AS $guard$\n{body.strip()}\n$guard$"
    )


def _raise(message: str, *args: str) -> str:
    params = "".join(f", {a}" for a in args)
    return (
        f"RAISE EXCEPTION 'guard: {message}'{params} "
        f"USING ERRCODE = '{GUARD_ERRCODE}';"
    )


# -- context ---------------------------------------------------------------

_TOKEN_DIGEST = _fn(
    "guard_token_digest",
    "p_token text",
    "text",
    """
BEGIN
    -- secrets.token_urlsafe(32): exactly 43 URL-safe characters. The error
    -- never repeats the input.
    IF p_token !~ '^[A-Za-z0-9_-]{43}$' THEN
        RAISE EXCEPTION 'guard: malformed session token'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    RETURN encode(sha256(convert_to(p_token, 'UTF8')), 'hex');
END
""",
    extra="IMMUTABLE STRICT\n",
)

# The predicate is smm_auth.session_resolve's, so the two never disagree on
# which session is live (tests/test_db_guards.py holds them together).
_BIND_SESSION = _fn(
    "guard_bind_session",
    "p_token text",
    "uuid",
    f"""
DECLARE
    v_hash text;
    v_session uuid;
    v_user uuid;
    v_txid bigint;
    v_bound uuid;
BEGIN
    IF p_token IS NULL THEN
        RETURN NULL;
    END IF;

    v_hash := public.guard_token_digest(p_token);

    SELECT s.id, s.user_id
      INTO v_session, v_user
      FROM public.user_sessions AS s
      JOIN public.users AS u ON u.id = s.user_id
     WHERE s.token_hash = v_hash
       AND s.revoked_at IS NULL
       AND s.expires_at > now()
       AND s.last_seen_at + interval '24 hours' > now()
       AND u.is_active
       AND s.created_at <= now() + interval '60 seconds'
       AND s.last_seen_at <= now() + interval '60 seconds'
       AND s.last_seen_at >= s.created_at - interval '60 seconds'
       AND s.expires_at - s.created_at <= interval '7 days 60 seconds';

    IF v_session IS NULL THEN
        RETURN NULL;
    END IF;

    v_txid := pg_current_xact_id()::text::bigint;

    SELECT c.user_id INTO v_bound
      FROM public.mutation_contexts AS c
     WHERE c.txid = v_txid;

    IF v_bound IS NOT NULL THEN
        IF v_bound <> v_user THEN
            {_raise('this transaction is already bound to another user')}
        END IF;

        RETURN v_user;
    END IF;

    INSERT INTO public.mutation_contexts (txid, user_id, session_id, bound_at)
    VALUES (v_txid, v_user, v_session, now());

    -- The contexts of finished transactions are inert; keep the table small.
    DELETE FROM public.mutation_contexts AS c
     WHERE c.bound_at < now() - interval '1 day';

    RETURN v_user;
END
""",
    extra="VOLATILE\nSECURITY DEFINER\n",
)

_CONTEXT_USER = _fn(
    "guard_context_user",
    "",
    "uuid",
    """
BEGIN
    RETURN (
        SELECT c.user_id
          FROM public.mutation_contexts AS c
         WHERE c.txid = pg_current_xact_id()::text::bigint
    );
END
""",
    extra="VOLATILE\nSECURITY DEFINER\n",
)

_CONTEXT_SESSION = _fn(
    "guard_context_session",
    "",
    "uuid",
    """
BEGIN
    RETURN (
        SELECT c.session_id
          FROM public.mutation_contexts AS c
         WHERE c.txid = pg_current_xact_id()::text::bigint
    );
END
""",
    extra="VOLATILE\nSECURITY DEFINER\n",
)

_REQUIRE_OWNER = _fn(
    "guard_require_owner",
    "p_project_id text, p_what text",
    "uuid",
    f"""
DECLARE
    v_user uuid := public.guard_context_user();
BEGIN
    IF v_user IS NULL THEN
        {_raise('% needs a session-bound transaction (guard_bind_session)', 'p_what')}
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM public.project_memberships AS m
         WHERE m.project_id = p_project_id
           AND m.user_id = v_user
           AND m.role = 'owner'
    ) THEN
        {_raise('% in project % is reserved to its owner', 'p_what', 'p_project_id')}
    END IF;

    RETURN v_user;
END
""",
    extra="VOLATILE\n",
)

# -- lineage ---------------------------------------------------------------

# NULL when the publication may be delivered, else the reason. Used by the
# COMMIT-time check of a new publication, by scheduling, and by the worker
# right before any external call (ai_smm.publishing.service).
_PUBLICATION_PROBLEM = _fn(
    "guard_publication_problem",
    "p_publication_id bigint",
    "text",
    """
DECLARE
    p public.publications%ROWTYPE;
    l public.content_publication_links%ROWTYPE;
    it public.content_items%ROWTYPE;
    r public.content_revisions%ROWTYPE;
    a public.content_approvals%ROWTYPE;
BEGIN
    SELECT * INTO p FROM public.publications WHERE id = p_publication_id;

    IF NOT FOUND THEN
        RETURN 'publication not found';
    END IF;

    IF NOT p.human_reviewed THEN
        RETURN 'not human-reviewed';
    END IF;

    SELECT * INTO l
      FROM public.content_publication_links
     WHERE publication_id = p.id;

    IF NOT FOUND THEN
        -- A row from before the editorial layer (importer, operator CLI):
        -- approved by the operator's own path, and always account-less.
        IF p.social_account_id IS NOT NULL THEN
            RETURN 'an account-bound publication has no content lineage';
        END IF;

        RETURN NULL;
    END IF;

    SELECT * INTO it FROM public.content_items WHERE id = l.content_item_id;
    SELECT * INTO r FROM public.content_revisions WHERE id = l.revision_id;

    IF r.id IS NULL OR r.content_item_id IS DISTINCT FROM it.id THEN
        RETURN 'the linked revision is not a revision of the linked item';
    END IF;

    IF it.project_id IS DISTINCT FROM p.project_id
       OR l.platform IS DISTINCT FROM p.platform THEN
        RETURN 'the lineage is in another project or on another platform';
    END IF;

    -- The latest decision about exactly this revision, in the order
    -- ai_smm.application.content._latest_decision reads it.
    SELECT * INTO a
      FROM public.content_approvals
     WHERE content_item_id = it.id
       AND revision_id = r.id
     ORDER BY created_at DESC, id
     LIMIT 1;

    IF NOT FOUND OR a.decision <> 'approved' OR a.actor_user_id IS NULL THEN
        RETURN 'no human approval of the linked revision';
    END IF;

    IF p.title IS DISTINCT FROM coalesce(nullif(r.title, ''), it.title)
       OR p.body IS DISTINCT FROM r.body
       OR p.format IS DISTINCT FROM r.format
       OR p.images IS DISTINCT FROM r.images
       OR p.items IS DISTINCT FROM r.items THEN
        RETURN 'the publication differs from the approved revision';
    END IF;

    IF p.social_account_id IS DISTINCT FROM r.social_account_id THEN
        RETURN 'the publication goes to another account than the approved one';
    END IF;

    RETURN NULL;
END
""",
    extra="STABLE\n",
)

# Consume the user's fresh step-up grant for this revision; refuse without
# one. Called by the approval trigger; callable directly by the business
# role, which can only waste its own grant that way.
_CONSUME_GRANT = _fn(
    "guard_consume_approval_grant",
    "p_approval_id uuid, p_content_item_id uuid, p_revision_id uuid",
    "uuid",
    f"""
DECLARE
    v_user uuid := public.guard_context_user();
    v_session uuid := public.guard_context_session();
    v_account uuid;
    v_identity text;
    g public.approval_grants%ROWTYPE;
BEGIN
    IF v_user IS NULL THEN
        {_raise('approving needs a session-bound transaction')}
    END IF;

    SELECT r.social_account_id INTO v_account
      FROM public.content_revisions AS r
     WHERE r.id = p_revision_id
       AND r.content_item_id = p_content_item_id;

    SELECT * INTO g
      FROM public.approval_grants AS ag
     WHERE ag.user_id = v_user
       AND ag.session_id = v_session
       AND ag.content_item_id = p_content_item_id
       AND ag.revision_id = p_revision_id
       AND ag.consumed_at IS NULL
       AND ag.expires_at > now()
     ORDER BY ag.created_at DESC, ag.id
     LIMIT 1
       FOR UPDATE;

    IF NOT FOUND THEN
        {_raise('approving revision % needs a fresh step-up confirmation of the owner (smm-auth)', 'p_revision_id')}
    END IF;

    IF g.social_account_id IS DISTINCT FROM v_account THEN
        {_raise('the confirmation was for another destination account')}
    END IF;

    IF v_account IS NOT NULL THEN
        SELECT a.external_account_id INTO v_identity
          FROM public.social_accounts AS a
         WHERE a.id = v_account;

        IF v_identity IS DISTINCT FROM g.external_account_id THEN
            {_raise('the destination account changed identity since the confirmation')}
        END IF;
    END IF;

    UPDATE public.approval_grants AS ag
       SET consumed_at = now(),
           approval_id = p_approval_id
     WHERE ag.id = g.id;

    RETURN g.id;
END
""",
    extra="VOLATILE\nSECURITY DEFINER\n",
)

# -- triggers ----------------------------------------------------------------


def _trigger_fn(name: str, body: str, *, declare: str = "") -> str:
    declare_block = f"DECLARE\n{declare.strip()}\n" if declare.strip() else ""

    return (
        f"CREATE OR REPLACE FUNCTION public.{name}() RETURNS trigger\n"
        f"{_HEADER}AS $guard$\n{declare_block}BEGIN\n"
        f"    IF {_GUARDED} THEN\n{body.rstrip()}\n    END IF;\n\n"
        f"    RETURN NEW;\nEND\n$guard$"
    )


_AUDIT_INSERT = (
    "CREATE OR REPLACE FUNCTION public.guard_audit_log_insert() RETURNS trigger\n"
    f"{_HEADER}AS $guard$\n"
    "DECLARE\n    v_user uuid;\nBEGIN\n"
    "    -- Provenance for every row, whoever writes it (FU-024C2-2). Inside a\n"
    "    -- SECURITY DEFINER function this is the role that called it.\n"
    "    NEW.written_by := session_user;\n\n"
    f"    IF NOT ({_GUARDED}) THEN\n        RETURN NEW;\n    END IF;\n\n"
    "    -- Column-level grants count: has_table_privilege would miss them.\n"
    "    IF pg_catalog.has_any_column_privilege(current_user, 'public.content_items', 'INSERT')\n"
    "       OR pg_catalog.has_any_column_privilege(current_user, 'public.publications', 'UPDATE') THEN\n"
    "        -- A business role: business actions, as the bound user, only.\n"
    "        v_user := public.guard_context_user();\n\n"
    "        IF v_user IS NULL THEN\n"
    f"            {_raise('audit entries of % need a session-bound transaction', 'current_user')}\n"
    "        END IF;\n\n"
    "        IF NEW.actor IS DISTINCT FROM 'user:' || v_user::text THEN\n"
    f"            {_raise('the audit actor must be the bound user')}\n"
    "        END IF;\n\n"
    "        IF NOT (" + " OR ".join(
        [f"NEW.action LIKE '{p}'" for p in BUSINESS_AUDIT_ACTIONS]
        + [f"NEW.action = '{e}'" for e in BUSINESS_AUDIT_EXACT]
    ) + ") THEN\n"
    f"            {_raise('audit action % is not a business action', 'NEW.action')}\n"
    "        END IF;\n"
    "    ELSIF NEW.action NOT LIKE 'auth.%' THEN\n"
    "        -- A role without business rights (the read-only API until\n"
    "        -- smm-auth takes over logins): sign-in events only.\n"
    f"        {_raise('% may write auth.* audit entries only', 'current_user')}\n"
    "    END IF;\n\n"
    "    RETURN NEW;\nEND\n$guard$"
)

_PROJECTS_INSERT = _trigger_fn(
    "guard_projects_insert",
    f"""
        v_user := public.guard_context_user();

        IF v_user IS NULL THEN
            {_raise('creating a project needs a session-bound transaction')}
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM public.project_memberships AS m
             WHERE m.user_id = v_user AND m.role = 'owner'
        ) THEN
            {_raise('only an existing project owner may create a project')}
        END IF;

        -- The transaction's own timestamp: how the membership and settings
        -- guards recognise a project created in this transaction.
        NEW.created_at := now();
        NEW.updated_at := now();
""",
    declare="    v_user uuid;",
)

_PROJECTS_UPDATE = _trigger_fn(
    "guard_projects_update",
    """
        PERFORM public.guard_require_owner(OLD.id, 'changing a project');
""",
)

_MEMBERSHIPS_INSERT = _trigger_fn(
    "guard_project_memberships_insert",
    f"""
        v_user := public.guard_context_user();

        IF v_user IS NULL OR NEW.user_id IS DISTINCT FROM v_user
           OR NEW.role <> 'owner' THEN
            {_raise('a membership is only ever the bound user becoming owner of a new project')}
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM public.projects AS p
             WHERE p.id = NEW.project_id AND p.created_at = now()
        ) OR EXISTS (
            SELECT 1 FROM public.project_memberships AS m
             WHERE m.project_id = NEW.project_id
        ) THEN
            {_raise('memberships of existing projects are not changed through the Control Center')}
        END IF;
""",
    declare="    v_user uuid;",
)

_SETTINGS_WRITE = _trigger_fn(
    "guard_project_settings_write",
    """
        IF TG_OP = 'INSERT' AND EXISTS (
            SELECT 1 FROM public.projects AS p
             WHERE p.id = NEW.project_id AND p.created_at = now()
        ) AND public.guard_context_user() IS NOT NULL THEN
            -- The settings row of a project created in this transaction.
            RETURN NEW;
        END IF;

        PERFORM public.guard_require_owner(NEW.project_id, 'changing project settings');
""",
)

_ACCOUNTS_INSERT = _trigger_fn(
    "guard_social_accounts_insert",
    f"""
        v_user := public.guard_require_owner(NEW.project_id, 'adding a social account');

        IF NEW.created_by_user_id IS DISTINCT FROM v_user THEN
            {_raise('the creator must be the bound user')}
        END IF;

        IF NEW.external_account_id IS NOT NULL THEN
            {_raise('an identity is bound by a verified credential, not at creation')}
        END IF;
""",
    declare="    v_user uuid;",
)

_ACCOUNTS_UPDATE = _trigger_fn(
    "guard_social_accounts_update",
    f"""
        PERFORM public.guard_require_owner(OLD.project_id, 'changing a social account');

        IF OLD.external_account_id IS NOT NULL
           AND NEW.external_account_id IS DISTINCT FROM OLD.external_account_id THEN
            {_raise('the Threads identity of social account % is bound and never changes', 'OLD.id')}
        END IF;
""",
)

_CREDENTIALS_INSERT = _trigger_fn(
    "guard_credentials_insert",
    f"""
        SELECT a.project_id INTO v_project
          FROM public.social_accounts AS a
         WHERE a.id = NEW.social_account_id;

        v_user := public.guard_require_owner(v_project, 'storing a credential');

        IF NEW.created_by_user_id IS DISTINCT FROM v_user
           OR NEW.status <> 'active'
           OR NEW.superseded_at IS NOT NULL
           OR NEW.revoked_at IS NOT NULL THEN
            {_raise('a new credential is active and created by the bound user')}
        END IF;
""",
    declare="    v_user uuid;\n    v_project text;",
)

_CREDENTIALS_UPDATE = _trigger_fn(
    "guard_credentials_update",
    f"""
        SELECT a.project_id INTO v_project
          FROM public.social_accounts AS a
         WHERE a.id = OLD.social_account_id;

        PERFORM public.guard_require_owner(v_project, 'retiring a credential');

        IF OLD.status <> 'active' OR NEW.status NOT IN ('superseded', 'revoked') THEN
            {_raise('a credential only goes from active to superseded or revoked')}
        END IF;
""",
    declare="    v_project text;",
)

_ITEMS_INSERT = _trigger_fn(
    "guard_content_items_insert",
    f"""
        v_user := public.guard_require_owner(NEW.project_id, 'creating content');

        IF NEW.created_by_user_id IS DISTINCT FROM v_user THEN
            {_raise('the creator must be the bound user')}
        END IF;
""",
    declare="    v_user uuid;",
)

_ITEMS_UPDATE = _trigger_fn(
    "guard_content_items_update",
    """
        PERFORM public.guard_require_owner(OLD.project_id, 'changing content');
""",
)

# COMMIT time: an item that says "approved" has a genuine approval of that
# revision -- the approval row is inserted in the same transaction, in an
# order the application does not promise.
_ITEMS_APPROVED_CHECK = (
    "CREATE OR REPLACE FUNCTION public.guard_content_items_approved_check() "
    "RETURNS trigger\n"
    f"{_HEADER}AS $guard$\n"
    "DECLARE\n    v_decision text;\nBEGIN\n"
    f"    IF NOT ({_GUARDED}) OR NEW.status <> 'approved' THEN\n"
    "        RETURN NULL;\n    END IF;\n\n"
    "    SELECT a.decision::text INTO v_decision\n"
    "      FROM public.content_approvals AS a\n"
    "     WHERE a.content_item_id = NEW.id\n"
    "       AND a.revision_id = NEW.approved_revision_id\n"
    "     ORDER BY a.created_at DESC, a.id\n"
    "     LIMIT 1;\n\n"
    "    IF v_decision IS DISTINCT FROM 'approved' THEN\n"
    f"        {_raise('content item % is approved without a human approval of its revision', 'NEW.id')}\n"
    "    END IF;\n\n"
    "    RETURN NULL;\nEND\n$guard$"
)

_REVISIONS_INSERT = _trigger_fn(
    "guard_content_revisions_insert",
    f"""
        SELECT i.project_id INTO v_project
          FROM public.content_items AS i
         WHERE i.id = NEW.content_item_id;

        v_user := public.guard_require_owner(v_project, 'writing a revision');

        IF NEW.created_by_user_id IS DISTINCT FROM v_user THEN
            {_raise('the author must be the bound user')}
        END IF;

        IF NEW.social_account_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.social_accounts AS a
             WHERE a.id = NEW.social_account_id AND a.project_id = v_project
        ) THEN
            {_raise('the destination account is not an account of this project')}
        END IF;
""",
    declare="    v_user uuid;\n    v_project text;",
)

_APPROVALS_INSERT = _trigger_fn(
    "guard_content_approvals_insert",
    f"""
        SELECT i.project_id, i.current_revision_id INTO v_project, v_current
          FROM public.content_items AS i
         WHERE i.id = NEW.content_item_id;

        v_user := public.guard_require_owner(v_project, 'deciding on content');

        IF NEW.actor_user_id IS DISTINCT FROM v_user THEN
            {_raise('the approver must be the bound user')}
        END IF;

        IF NEW.revision_id IS DISTINCT FROM v_current THEN
            {_raise('only the current revision can be decided')}
        END IF;

        NEW.created_at := now();

        IF NEW.decision = 'approved' THEN
            PERFORM public.guard_consume_approval_grant(
                NEW.id, NEW.content_item_id, NEW.revision_id);
        END IF;
""",
    declare="    v_user uuid;\n    v_project text;\n    v_current uuid;",
)

_LINKS_INSERT = _trigger_fn(
    "guard_content_links_insert",
    f"""
        SELECT i.project_id, i.approved_revision_id INTO v_project, v_approved
          FROM public.content_items AS i
         WHERE i.id = NEW.content_item_id;

        PERFORM public.guard_require_owner(v_project, 'linking a publication');

        IF NEW.revision_id IS DISTINCT FROM v_approved THEN
            {_raise('only the approved revision is materialised')}
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM public.publications AS p
             WHERE p.id = NEW.publication_id AND p.created_at = now()
        ) THEN
            {_raise('lineage is only ever given to a publication created in the same transaction')}
        END IF;
""",
    declare="    v_project text;\n    v_approved uuid;",
)

_PUBLICATIONS_INSERT = _trigger_fn(
    "guard_publications_insert",
    f"""
        PERFORM public.guard_require_owner(NEW.project_id, 'creating a publication');

        IF NEW.status <> 'approved'
           OR NOT NEW.human_reviewed
           OR NEW.scheduled_at IS NOT NULL
           OR NEW.claimed_by IS NOT NULL
           OR NEW.claimed_at IS NOT NULL
           OR NEW.lease_expires_at IS NOT NULL
           OR NEW.threads_post_id IS NOT NULL
           OR NEW.published_at IS NOT NULL
           OR NEW.next_attempt_at IS NOT NULL
           OR NEW.last_error IS NOT NULL
           OR NEW.attempt_count <> 0
           OR NEW.series_id IS NOT NULL
           OR NEW.series_position IS NOT NULL
           OR NEW.series_total IS NOT NULL
           OR NEW.parent_publication_id IS NOT NULL
           OR NEW.previous_publication_id IS NOT NULL THEN
            {_raise('a new publication is an approved, unscheduled delivery snapshot')}
        END IF;

        NEW.created_at := now();
        NEW.updated_at := now();
""",
)

# COMMIT time: the new snapshot carries valid lineage (the link row is
# written after the publication).
_PUBLICATIONS_LINEAGE_CHECK = (
    "CREATE OR REPLACE FUNCTION public.guard_publications_lineage_check() "
    "RETURNS trigger\n"
    f"{_HEADER}AS $guard$\n"
    "DECLARE\n    v_problem text;\nBEGIN\n"
    f"    IF NOT ({_GUARDED}) THEN\n        RETURN NULL;\n    END IF;\n\n"
    "    -- A row this role creates is a content snapshot, never a legacy row:\n"
    "    -- without a link it would pass as one and be scheduled unapproved.\n"
    "    IF NOT EXISTS (SELECT 1 FROM public.content_publication_links AS l\n"
    "                    WHERE l.publication_id = NEW.id) THEN\n"
    f"        {_raise('publication % has no content lineage', 'NEW.id')}\n"
    "    END IF;\n\n"
    "    v_problem := public.guard_publication_problem(NEW.id);\n\n"
    "    IF v_problem IS NOT NULL THEN\n"
    f"        {_raise('publication %: %', 'NEW.id', 'v_problem')}\n"
    "    END IF;\n\n"
    "    RETURN NULL;\nEND\n$guard$"
)

_PUBLICATIONS_UPDATE = _trigger_fn(
    "guard_publications_update",
    f"""
        PERFORM public.guard_require_owner(OLD.project_id, 'changing a publication');

        -- The three queue commands (ai_smm.application.publications
        -- COMMAND_POLICY), never a claim or a delivery outcome.
        IF NOT (
            (OLD.status IN ('approved', 'failed') AND NEW.status = 'scheduled')
            OR (OLD.status IN ('scheduled', 'failed') AND NEW.status = 'scheduled')
            OR (OLD.status IN ('draft', 'approved', 'scheduled', 'failed')
                AND NEW.status = 'cancelled')
        ) THEN
            {_raise('publication %: % -> % is not a Control Center command', 'OLD.id', 'OLD.status', 'NEW.status')}
        END IF;

        IF NEW.claimed_by IS NOT NULL
           OR NEW.claimed_at IS NOT NULL
           OR NEW.lease_expires_at IS NOT NULL
           OR NEW.next_attempt_at IS NOT NULL
           OR NEW.attempt_count NOT IN (OLD.attempt_count, 0)
           OR (NEW.last_error IS NOT NULL
               AND NEW.last_error IS DISTINCT FROM OLD.last_error) THEN
            {_raise('publication %: claim, lease and outcome columns belong to the worker', 'OLD.id')}
        END IF;

        IF NEW.status = 'scheduled' THEN
            IF OLD.threads_post_id IS NOT NULL OR OLD.published_at IS NOT NULL
               OR NEW.scheduled_at IS NULL THEN
                {_raise('publication % cannot be scheduled', 'OLD.id')}
            END IF;

            v_problem := public.guard_publication_problem(OLD.id);

            IF v_problem IS NOT NULL THEN
                {_raise('publication %: %', 'OLD.id', 'v_problem')}
            END IF;

            IF NEW.social_account_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.social_accounts AS a
                 WHERE a.id = NEW.social_account_id
                   AND a.project_id = NEW.project_id
                   AND a.status = 'active'
                   AND a.external_account_id IS NOT NULL
            ) THEN
                {_raise('publication %: its account is not ready to publish', 'OLD.id')}
            END IF;
        END IF;
""",
    declare="    v_problem text;",
)


#: Version 1: SMM-025D. Order matters: helpers before their callers.
FUNCTIONS_V1: tuple[GuardFunction, ...] = (
    GuardFunction("guard_token_digest", "text", _TOKEN_DIGEST),
    GuardFunction("guard_bind_session", "text", _BIND_SESSION, True),
    GuardFunction("guard_context_user", "", _CONTEXT_USER, True),
    GuardFunction("guard_context_session", "", _CONTEXT_SESSION, True),
    GuardFunction("guard_require_owner", "text, text", _REQUIRE_OWNER, True),
    GuardFunction("guard_publication_problem", "bigint", _PUBLICATION_PROBLEM, True),
    GuardFunction(
        "guard_consume_approval_grant", "uuid, uuid, uuid", _CONSUME_GRANT, True
    ),
    GuardFunction("guard_audit_log_insert", "", _AUDIT_INSERT),
    GuardFunction("guard_projects_insert", "", _PROJECTS_INSERT),
    GuardFunction("guard_projects_update", "", _PROJECTS_UPDATE),
    GuardFunction("guard_project_memberships_insert", "", _MEMBERSHIPS_INSERT),
    GuardFunction("guard_project_settings_write", "", _SETTINGS_WRITE),
    GuardFunction("guard_social_accounts_insert", "", _ACCOUNTS_INSERT),
    GuardFunction("guard_social_accounts_update", "", _ACCOUNTS_UPDATE),
    GuardFunction("guard_credentials_insert", "", _CREDENTIALS_INSERT),
    GuardFunction("guard_credentials_update", "", _CREDENTIALS_UPDATE),
    GuardFunction("guard_content_items_insert", "", _ITEMS_INSERT),
    GuardFunction("guard_content_items_update", "", _ITEMS_UPDATE),
    GuardFunction(
        "guard_content_items_approved_check", "", _ITEMS_APPROVED_CHECK
    ),
    GuardFunction("guard_content_revisions_insert", "", _REVISIONS_INSERT),
    GuardFunction("guard_content_approvals_insert", "", _APPROVALS_INSERT),
    GuardFunction("guard_content_links_insert", "", _LINKS_INSERT),
    GuardFunction("guard_publications_insert", "", _PUBLICATIONS_INSERT),
    GuardFunction(
        "guard_publications_lineage_check", "", _PUBLICATIONS_LINEAGE_CHECK
    ),
    GuardFunction("guard_publications_update", "", _PUBLICATIONS_UPDATE),
)


def _before(name: str, table: str, op: str, fn: str) -> GuardTrigger:
    return GuardTrigger(
        name,
        table,
        f"CREATE TRIGGER {name} BEFORE {op} ON public.{table} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{fn}()",
    )


def _deferred(name: str, table: str, op: str, fn: str) -> GuardTrigger:
    return GuardTrigger(
        name,
        table,
        f"CREATE CONSTRAINT TRIGGER {name} AFTER {op} ON public.{table} "
        "DEFERRABLE INITIALLY DEFERRED "
        f"FOR EACH ROW EXECUTE FUNCTION public.{fn}()",
    )


TRIGGERS_V1: tuple[GuardTrigger, ...] = (
    _before("trg_audit_log_guard_insert", "audit_log", "INSERT", "guard_audit_log_insert"),
    _before("trg_projects_guard_insert", "projects", "INSERT", "guard_projects_insert"),
    _before("trg_projects_guard_update", "projects", "UPDATE", "guard_projects_update"),
    _before(
        "trg_project_memberships_guard_insert",
        "project_memberships",
        "INSERT",
        "guard_project_memberships_insert",
    ),
    _before(
        "trg_project_settings_guard_write",
        "project_settings",
        "INSERT OR UPDATE",
        "guard_project_settings_write",
    ),
    _before(
        "trg_social_accounts_guard_insert",
        "social_accounts",
        "INSERT",
        "guard_social_accounts_insert",
    ),
    _before(
        "trg_social_accounts_guard_update",
        "social_accounts",
        "UPDATE",
        "guard_social_accounts_update",
    ),
    _before(
        "trg_social_account_credentials_guard_insert",
        "social_account_credentials",
        "INSERT",
        "guard_credentials_insert",
    ),
    _before(
        "trg_social_account_credentials_guard_update",
        "social_account_credentials",
        "UPDATE",
        "guard_credentials_update",
    ),
    _before(
        "trg_content_items_guard_insert",
        "content_items",
        "INSERT",
        "guard_content_items_insert",
    ),
    _before(
        "trg_content_items_guard_update",
        "content_items",
        "UPDATE",
        "guard_content_items_update",
    ),
    _deferred(
        "trg_content_items_guard_approved",
        "content_items",
        "INSERT OR UPDATE",
        "guard_content_items_approved_check",
    ),
    _before(
        "trg_content_revisions_guard_insert",
        "content_revisions",
        "INSERT",
        "guard_content_revisions_insert",
    ),
    _before(
        "trg_content_approvals_guard_insert",
        "content_approvals",
        "INSERT",
        "guard_content_approvals_insert",
    ),
    _before(
        "trg_content_publication_links_guard_insert",
        "content_publication_links",
        "INSERT",
        "guard_content_links_insert",
    ),
    _before(
        "trg_publications_guard_insert",
        "publications",
        "INSERT",
        "guard_publications_insert",
    ),
    _deferred(
        "trg_publications_guard_lineage",
        "publications",
        "INSERT",
        "guard_publications_lineage_check",
    ),
    _before(
        "trg_publications_guard_update",
        "publications",
        "UPDATE",
        "guard_publications_update",
    ),
)


def revoke_public_statements(functions: tuple[GuardFunction, ...]) -> list[str]:
    """PostgreSQL grants EXECUTE on a new function to PUBLIC; take it away."""

    return [
        f"REVOKE ALL ON FUNCTION public.{f.name}({f.signature}) FROM PUBLIC"
        for f in functions
    ]


def drop_function_statements(functions: tuple[GuardFunction, ...]) -> list[str]:
    return [
        f"DROP FUNCTION IF EXISTS public.{f.name}({f.signature})"
        for f in reversed(functions)
    ]


def business_role_functions(functions: tuple[GuardFunction, ...]) -> list[str]:
    """Signatures the business role needs EXECUTE on (grants files)."""

    return [
        f"public.{f.name}({f.signature})"
        for f in functions
        if f.callable_by_business_role
    ]
