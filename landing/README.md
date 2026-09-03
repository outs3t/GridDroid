# GridDroid Landing Page

Sito di presentazione statico per **GridDroid**, pronto per essere pubblicato su [Vercel](https://vercel.com).

## Caratteristiche

- **Tema dark moderno**, animazioni e glassmorphism.
- Sezioni: hero, funzionalità, come funziona, download, donazione PayPal.
- Responsive (desktop, tablet, mobile).
- Zero dipendenze di build: HTML + CSS + JavaScript + Tailwind CDN.

## Pubblica su Vercel

1. Carica la cartella `landing` in un repository GitHub.
2. Vai su [vercel.com](https://vercel.com) → **Add New Project**.
3. Importa il repository e imposta la **Root Directory** su `landing`.
4. Clicca **Deploy**.

Vercel rileverà automaticamente il progetto statico (`index.html`).

## Personalizza

- Apri `index.html` e sostituisci:
  - `https://github.com/USERNAME/GridDroid/...` con il tuo repository/release.
  - `REPLACE_WITH_YOUR_ID` nei link PayPal con il tuo button ID o `paypal.me/...`.
- Modifica testi, colori e icone in `index.html` e `style.css`.

## Test in locale

```bash
cd landing
python -m http.server 8080
```

Poi visita `http://localhost:8080`.
