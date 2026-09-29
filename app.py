import os
import time
import threading
from urllib.parse import urlparse

from flask import Flask, request, redirect, url_for, render_template_string, flash
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "ticketing-secret-key-2024")

# ---------------------------------------------------------------------------
# Database connection management
# ---------------------------------------------------------------------------
# The app supports two connection modes:
#   1. OAuth via Databricks SDK — used when PGHOST / PGDATABASE env vars are
#      injected by the standard Lakebase app-resource integration, or when
#      the LAKEBASE_URL secret contains a Lakebase endpoint resource name
#      (e.g. "projects/my-project/branches/production/endpoints/primary").
#   2. Password-based auth — used when LAKEBASE_URL is a hostname or full
#      connection URL and API_KEY contains the password (e.g. a pg-native
#      role created on the Lakebase project).

_w = None                     # lazily-initialised WorkspaceClient (OAuth only)
_oauth_token = None
_token_refresh_time = 0
_token_lock = threading.Lock()

DB_HOST = None
DB_PORT = 5432
DB_NAME = "databricks_postgres"
DB_USER = "student"
DB_PASSWORD = None
USE_OAUTH = False
ENDPOINT_NAME = None
SCHEMA_NAME = "ticketing_system"


def _init_db_config():
    """Detect connection parameters from environment variables / secrets."""
    global DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, USE_OAUTH, ENDPOINT_NAME

    # Mode 1 — standard Lakebase app-resource env vars (OAuth)
    if os.environ.get("PGHOST"):
        DB_HOST = os.environ["PGHOST"]
        DB_NAME = os.environ.get("PGDATABASE", "databricks_postgres")
        DB_USER = os.environ.get("PGUSER", "")
        DB_PORT = int(os.environ.get("PGPORT", 5432))
        USE_OAUTH = True
        return

    lakebase_url = os.environ.get("LAKEBASE_URL", "")
    api_key = os.environ.get("API_KEY", "")

    # Mode 2 — LAKEBASE_URL is a Lakebase endpoint resource name (OAuth)
    if lakebase_url.startswith("projects/"):
        ENDPOINT_NAME = lakebase_url
        USE_OAUTH = True
        # Host will be resolved from the endpoint at first connection
        return

    # Mode 3 — LAKEBASE_URL is a full postgres connection URL
    if lakebase_url and "://" in lakebase_url:
        parsed = urlparse(lakebase_url)
        DB_HOST = parsed.hostname
        DB_PORT = parsed.port or 5432
        DB_NAME = parsed.path.lstrip("/") or "databricks_postgres"
        DB_USER = parsed.username or "student"
        DB_PASSWORD = parsed.password or api_key
        return

    # Mode 4 — LAKEBASE_URL is a bare hostname (password auth)
    if lakebase_url:
        DB_HOST = lakebase_url
        DB_PASSWORD = api_key
        return

    raise RuntimeError(
        "No database configuration found. Set PGHOST env var or "
        "LAKEBASE_URL / API_KEY secrets."
    )


def _get_oauth_password():
    """Generate / refresh an OAuth token for Lakebase via the Databricks SDK."""
    global _w, _oauth_token, _token_refresh_time
    with _token_lock:
        if _oauth_token is None or time.time() - _token_refresh_time > 900:
            if _w is None:
                from databricks.sdk import WorkspaceClient
                _w = WorkspaceClient()
            if not ENDPOINT_NAME:
                raise RuntimeError(
                    "OAuth mode requires a Lakebase endpoint name. "
                    "Set LAKEBASE_URL to the endpoint resource path."
                )
            cred = _w.postgres.generate_database_credential(endpoint=ENDPOINT_NAME)
            _oauth_token = cred.token
            _token_refresh_time = time.time()
        return _oauth_token


def get_connection():
    """Return a psycopg2 connection to the Lakebase Postgres database."""
    _init_db_config()
    password = _get_oauth_password() if USE_OAUTH else DB_PASSWORD
    return psycopg2.connect(
        host=DB_HOST,
        database=DB_NAME,
        user=DB_USER,
        port=DB_PORT,
        password=password,
        sslmode="require",
        connect_timeout=30,
    )


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_schema():
    """Create the ticketing schema and tables if they don't already exist."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME}")
            cur.execute(f"DROP TABLE IF EXISTS {SCHEMA_NAME}.ticket_messages")
            cur.execute(f"DROP TABLE IF EXISTS {SCHEMA_NAME}.tickets")
            cur.execute(f"""
                CREATE TABLE {SCHEMA_NAME}.tickets (
                    ticket_id   SERIAL PRIMARY KEY,
                    title       VARCHAR(255) NOT NULL,
                    status      VARCHAR(50)  NOT NULL DEFAULT 'open',
                    created_by  VARCHAR(255),
                    created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(f"""
                CREATE TABLE {SCHEMA_NAME}.ticket_messages (
                    message_id   SERIAL PRIMARY KEY,
                    ticket_id    INTEGER NOT NULL
                                 REFERENCES {SCHEMA_NAME}.tickets(ticket_id) ON DELETE CASCADE,
                    message_text TEXT         NOT NULL,
                    author       VARCHAR(100) NOT NULL,
                    created_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_messages_ticket_id
                ON {SCHEMA_NAME}.ticket_messages(ticket_id)
            """)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HTML Templates (inline, Bootstrap 5)
# ---------------------------------------------------------------------------

BASE_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Day One Ticketing System</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css" rel="stylesheet">
  <style>
    body { background: #f5f7fa; }
    .navbar { background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%); }
    .navbar a { color: #fff; text-decoration: none; font-weight: 600; }
    .card { border: none; box-shadow: 0 2px 8px rgba(0,0,0,.08); border-radius: 12px; }
    .badge-open { background: #f59e0b; }
    .badge-in-progress { background: #3b82f6; }
    .badge-resolved { background: #10b981; }
    .badge-closed { background: #6b7280; }
    .message-bubble { background: #eef2ff; border-radius: 12px; padding: 12px 16px; margin-bottom: 12px; }
    .message-meta { font-size: .8rem; color: #6b7280; }
  </style>
</head>
<body>
  <nav class="navbar navbar-expand-lg mb-4 py-3">
    <div class="container">
      <a class="navbar-brand" href="{{ url_for('list_tickets') }}">
        <i class="bi bi-ticket-detailed"></i> Day One Ticketing System
      </a>
      <div class="d-flex gap-3">
        <a href="{{ url_for('list_tickets') }}" class="nav-link">All Tickets</a>
        <a href="{{ url_for('new_ticket') }}" class="nav-link">New Ticket</a>
      </div>
    </div>
  </nav>
  <div class="container">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, message in messages %}
          <div class="alert alert-{{ 'danger' if category == 'error' else 'success' }} alert-dismissible fade show">
            {{ message }}
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
          </div>
        {% endfor %}
      {% endif %}
    {% endwith %}
    {% block content %}{% endblock %}
  </div>
  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>'''

LIST_TICKETS_HTML = BASE_HTML.replace(
    "{% block content %}{% endblock %}",
    """
    <div class="d-flex justify-content-between align-items-center mb-4">
      <h3><i class="bi bi-list-check"></i> Support Tickets</h3>
      <a href="{{ url_for('new_ticket') }}" class="btn btn-primary">
        <i class="bi bi-plus-lg"></i> New Ticket
      </a>
    </div>
    {% if tickets %}
    <div class="table-responsive">
      <table class="table table-hover align-middle">
        <thead class="table-light">
          <tr>
            <th>#</th>
            <th>Title</th>
            <th>Status</th>
            <th>Created By</th>
            <th>Created</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {% for t in tickets %}
          <tr>
            <td>{{ t.ticket_id }}</td>
            <td><a href="{{ url_for('ticket_detail', ticket_id=t.ticket_id) }}" class="text-decoration-none fw-semibold">{{ t.title }}</a></td>
            <td><span class="badge badge-{{ t.status|replace(' ', '-') }}">{{ t.status }}</span></td>
            <td>{{ t.created_by or '-' }}</td>
            <td>{{ t.created_at.strftime('%Y-%m-%d %H:%M') }}</td>
            <td><a href="{{ url_for('ticket_detail', ticket_id=t.ticket_id) }}" class="btn btn-sm btn-outline-primary">View</a></td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    {% else %}
    <div class="text-center py-5">
      <i class="bi bi-inbox text-secondary" style="font-size: 3rem;"></i>
      <h5 class="mt-3 text-secondary">No tickets yet</h5>
      <a href="{{ url_for('new_ticket') }}" class="btn btn-primary mt-2">Create your first ticket</a>
    </div>
    {% endif %}
    """
)

TICKET_DETAIL_HTML = BASE_HTML.replace(
    "{% block content %}{% endblock %}",
    """
    <div class="row">
      <div class="col-lg-8">
        <div class="card p-4 mb-4">
          <div class="d-flex justify-content-between align-items-start">
            <div>
              <h3>{{ ticket.title }}</h3>
              <span class="badge badge-{{ ticket.status|replace(' ', '-') }} mb-2">{{ ticket.status }}</span>
            </div>
            <a href="{{ url_for('list_tickets') }}" class="btn btn-sm btn-outline-secondary">&larr; Back</a>
          </div>
          <hr>
          <div class="d-flex gap-3 text-muted small">
            <span><i class="bi bi-person"></i> Created by: {{ ticket.created_by or '-' }}</span>
            <span><i class="bi bi-calendar"></i> Created: {{ ticket.created_at.strftime('%Y-%m-%d %H:%M') }}</span>
          </div>
        </div>

        <h5><i class="bi bi-chat-dots"></i> Messages</h5>
        {% if messages %}
          {% for msg in messages %}
          <div class="message-bubble">
            <div class="d-flex justify-content-between">
              <strong>{{ msg.author }}</strong>
              <span class="message-meta">{{ msg.created_at.strftime('%Y-%m-%d %H:%M') }}</span>
            </div>
            <p class="mb-0 mt-1">{{ msg.message_text }}</p>
          </div>
          {% endfor %}
        {% else %}
          <p class="text-muted">No messages yet. Start the conversation below.</p>
        {% endif %}

        <form method="post" action="{{ url_for('add_message', ticket_id=ticket.ticket_id) }}" class="card p-3 mt-3">
          <h6 class="mb-3">Add a Message</h6>
          <div class="mb-2">
            <input type="text" name="author" class="form-control" placeholder="Your name" required>
          </div>
          <div class="mb-2">
            <textarea name="message_text" class="form-control" rows="3" placeholder="Type your message..." required></textarea>
          </div>
          <button type="submit" class="btn btn-primary"><i class="bi bi-send"></i> Send Message</button>
        </form>
      </div>

      <div class="col-lg-4">
        <div class="card p-4">
          <h6>Update Status</h6>
          <form method="post" action="{{ url_for('update_status', ticket_id=ticket.ticket_id) }}">
            <select name="status" class="form-select mb-3">
              <option value="open" {{ 'selected' if ticket.status == 'open' else '' }}>Open</option>
              <option value="in progress" {{ 'selected' if ticket.status == 'in progress' else '' }}>In Progress</option>
              <option value="resolved" {{ 'selected' if ticket.status == 'resolved' else '' }}>Resolved</option>
              <option value="closed" {{ 'selected' if ticket.status == 'closed' else '' }}>Closed</option>
            </select>
            <button type="submit" class="btn btn-outline-primary w-100"><i class="bi bi-arrow-repeat"></i> Update Status</button>
          </form>
        </div>
      </div>
    </div>
    """
)

NEW_TICKET_HTML = BASE_HTML.replace(
    "{% block content %}{% endblock %}",
    """
    <h3><i class="bi bi-plus-circle"></i> Create New Ticket</h3>
    <div class="card p-4 mt-3" style="max-width: 700px;">
      <form method="post" action="{{ url_for('new_ticket') }}">
        <div class="mb-3">
          <label class="form-label">Title</label>
          <input type="text" name="title" class="form-control" required>
        </div>
        <div class="mb-3">
          <label class="form-label">Created By</label>
          <input type="text" name="created_by" class="form-control" placeholder="Your name">
        </div>
        <div class="d-flex gap-2">
          <button type="submit" class="btn btn-primary"><i class="bi bi-check-lg"></i> Create Ticket</button>
          <a href="{{ url_for('list_tickets') }}" class="btn btn-outline-secondary">Cancel</a>
        </div>
      </form>
    </div>
    """
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def list_tickets():
    """View all support tickets."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"""
                SELECT ticket_id, title, status, created_by, created_at
                FROM {SCHEMA_NAME}.tickets
                ORDER BY created_at DESC
            """)
            tickets = cur.fetchall()
        return render_template_string(LIST_TICKETS_HTML, tickets=tickets)
    finally:
        conn.close()


@app.route("/ticket/<int:ticket_id>")
def ticket_detail(ticket_id):
    """Select a ticket and view its messages."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT ticket_id, title, status, created_by, created_at "
                f"FROM {SCHEMA_NAME}.tickets WHERE ticket_id = %s",
                (ticket_id,),
            )
            ticket = cur.fetchone()
            if not ticket:
                flash("Ticket not found.", "error")
                return redirect(url_for("list_tickets"))

            cur.execute(
                f"SELECT message_id, ticket_id, message_text, author, created_at "
                f"FROM {SCHEMA_NAME}.ticket_messages WHERE ticket_id = %s "
                f"ORDER BY created_at ASC",
                (ticket_id,),
            )
            messages = cur.fetchall()
        return render_template_string(
            TICKET_DETAIL_HTML, ticket=ticket, messages=messages
        )
    finally:
        conn.close()


@app.route("/new", methods=["GET", "POST"])
def new_ticket():
    """Create a new ticket."""
    if request.method == "GET":
        return render_template_string(NEW_TICKET_HTML)

    title = request.form.get("title", "").strip()
    created_by = request.form.get("created_by", "").strip()
    if not title:
        flash("Title is required.", "error")
        return redirect(url_for("new_ticket"))

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {SCHEMA_NAME}.tickets (title, status, created_by) "
                f"VALUES (%s, 'open', %s) RETURNING ticket_id",
                (title, created_by),
            )
            new_id = cur.fetchone()[0]
        conn.commit()
        flash(f"Ticket #{new_id} created successfully.", "success")
        return redirect(url_for("ticket_detail", ticket_id=new_id))
    finally:
        conn.close()


@app.route("/ticket/<int:ticket_id>/message", methods=["POST"])
def add_message(ticket_id):
    """Add a message to an existing ticket."""
    author = request.form.get("author", "").strip()
    message_text = request.form.get("message_text", "").strip()
    if not author or not message_text:
        flash("Author and message are required.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT ticket_id FROM {SCHEMA_NAME}.tickets WHERE ticket_id = %s", (ticket_id,)
            )
            if not cur.fetchone():
                flash("Ticket not found.", "error")
                return redirect(url_for("list_tickets"))

            cur.execute(
                f"INSERT INTO {SCHEMA_NAME}.ticket_messages (ticket_id, author, message_text) "
                f"VALUES (%s, %s, %s)",
                (ticket_id, author, message_text),
            )
        conn.commit()
        flash("Message added.", "success")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    finally:
        conn.close()


@app.route("/ticket/<int:ticket_id>/status", methods=["POST"])
def update_status(ticket_id):
    """Update a ticket's status."""
    new_status = request.form.get("status", "open").strip()
    valid_statuses = {"open", "in progress", "resolved", "closed"}
    if new_status not in valid_statuses:
        flash("Invalid status.", "error")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {SCHEMA_NAME}.tickets SET status = %s "
                f"WHERE ticket_id = %s",
                (new_status, ticket_id),
            )
            if cur.rowcount == 0:
                flash("Ticket not found.", "error")
                return redirect(url_for("list_tickets"))
        conn.commit()
        flash(f"Status updated to '{new_status}'.", "success")
        return redirect(url_for("ticket_detail", ticket_id=ticket_id))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_schema()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
