---
title: Connections and Explorer
icon: CABLE
permission: use_connections
pages: [connections, explorer]
keywords: database, oracle, sql server, postgres, mysql, sftp, smb, s3, azure, folder, explorer, custom sql, driver
---
# Connections and Explorer

A **connection** tells DataBridge where data lives: a folder or share, a database, or a REST API (see [REST API sources](guide:rest-sources)). Admins create connections, because they hold passwords; designers can browse them.

## Add a connection (admins)

1. Go to [Connections](app:connections) and click **New connection**.
2. Choose the **Connection type**:
   - **File system**: local folders, SMB shares, SFTP, FTP, Amazon S3, Azure Blob and Google Cloud Storage. Set the **Protocol**, then the **Root** folder and host details.
   - **Database**: choose the **Dialect** (Oracle, SQL Server, PostgreSQL, MySQL, SQLite, ...), then host, port, database or service name, and credentials.
3. Click **Save and test**.

Tips:

- **Read-only account:** use a database account that can only read.
- **Driver hint:** the dialog says if the server is missing the driver for your database; an admin installs it on the server.
- **Schema allowlist:** limits which schemas show up in the Explorer.
- **Special options:** use **Options** (e.g. `driver=ODBC Driver 18 for SQL Server`) or **URL override** for unusual setups.
- **Passwords:** they are encrypted and never shown again. When editing, leave the password blank to keep it.

## Browse with the Explorer

1. Open [Explorer](app:explorer) and pick a connection.
2. Expand the tree to find a table, view, sheet or file. The right side shows its columns (with keys) and a preview of 100 rows.
3. Click **Add as source**. The object becomes a source that you can **Refresh** later.

### Custom SQL

For databases, **Custom SQL** lets you write a query. Only a single read-only `SELECT` is allowed. **Validate and preview** checks it before you save it as a source.
