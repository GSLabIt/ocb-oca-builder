<!-- markdownlint-disable MD024 MD041 -->
## Unreleased

### Feat

- **wkhtmltopdf** — l'immagine include ora `wkhtmltox 0.12.6.1-3` (build Qt
  patchata, richiesta da Odoo per header/footer nei report PDF); il `.deb`
  ufficiale è scelto per codename base e architettura.

### Fix

- **Entrypoint ownership** — l'entrypoint fa `chown` di
  `/var/lib/odoo/{filestore,sessions}` all'utente `odoo` prima di rilasciare i
  privilegi, così un filestore tenant bind-montato lasciato di proprietà di
  root da un container precedente non causa più `PermissionError` a ogni
  scrittura di attachment (istanza Odoo "corrotta"). Chowna solo le voci con
  proprietà errata, restando economico su un filestore grande già corretto.
