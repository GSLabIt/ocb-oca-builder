<!-- markdownlint-disable MD024 MD041 -->
## Unreleased

### Fix

- **Entrypoint ownership** — l'entrypoint fa `chown` di
  `/var/lib/odoo/{filestore,sessions}` all'utente `odoo` prima di rilasciare i
  privilegi, così un filestore tenant bind-montato lasciato di proprietà di
  root da un container precedente non causa più `PermissionError` a ogni
  scrittura di attachment (istanza Odoo "corrotta"). Chowna solo le voci con
  proprietà errata, restando economico su un filestore grande già corretto.
