# F95 Library

Libreria web self-hosted per organizzare i thread di giochi F95Zone. La prima versione importa titolo, copertina, galleria immagini, overview, versione corrente e statistiche del thread quando sono disponibili; consente ricerca, stato personale, commenti e controllo periodico delle modifiche ai thread.

Non scarica, installa o avvia giochi. Non è affiliata a F95Zone.

## Avvio con Docker

1. Installa Docker Desktop.
2. Nella cartella del progetto, avvia `docker compose up --build -d`.
3. Apri [http://localhost:8080](http://localhost:8080).

I dati sono conservati nel volume Docker `f95-library-data`. Per fermare l'app usa `docker compose down`; il volume non viene eliminato.

Per una verifica manuale dei log: `docker compose logs -f`.

## Modalità sviluppo (hot reload)

Per sviluppare senza ricreare l'immagine a ogni modifica, avvia Docker Desktop e usa:

```powershell
docker compose -f compose.yaml -f compose.dev.yaml up -d
```

Il sorgente viene montato nel container: modifiche a HTML/CSS/JavaScript sono ricaricate dal browser automaticamente; modifiche ai file Python riavviano il server. Apri o aggiorna [http://localhost:8080](http://localhost:8080). Per tornare alla modalità standard: `docker compose down`, poi `docker compose up -d --build`.

In alternativa, fuori da Docker, esegui `python dev.py` e apri lo stesso indirizzo locale della modalità server.

## Accesso a F95Zone

Apri **Impostazioni** (icona ⚙) e accedi con il tuo account F95Zone. L'app inoltra la password a F95Zone via HTTPS per il login e non la salva. Vengono memorizzati nel volume locale solo i cookie della sessione; la funzione **Disconnetti** li rimuove. La sessione serve per leggere i metadati dei thread che richiedono l'accesso, non per scaricare giochi.

I cookie di sessione sono credenziali temporanee: l'app li conserva nel database SQLite del volume `/data` con permessi limitati e li invia solo a F95Zone. Proteggi il volume e non condividerlo. Il servizio è vincolato a `localhost`; non esporlo direttamente su Internet. Se configuri accesso remoto, usa un reverse proxy con autenticazione e HTTPS. Il login potrebbe richiedere passaggi aggiuntivi (per esempio CAPTCHA o verifica a due fattori) non gestiti dall'MVP.

Il controllo automatico parte all'avvio e poi avviene ogni 6 ore per limitare le richieste. Se F95Zone rifiuta la sessione, l'errore è visibile nella scheda: accedi di nuovo da **Impostazioni**.

Per i dettagli degli aggiornamenti e gli eventuali contenuti completi, apri il thread originale da una scheda gioco. Il rilevamento automatico segnala un update quando riconosce una data di post più recente nella pagina.

## Dati e limiti dell'MVP

- Database SQLite nel volume `/data`.
- Importazione tramite URL del thread.
- Copertina, galleria e overview vengono estratte dal primo post del thread. L'overview si interrompe prima dei campi metadata successivi (come "Thread Updated"). Le immagini ospitate sui domini F95Zone passano da un proxy locale che invia il Referer richiesto e limita host, tipo e dimensione della risposta.
- Stato ed engine vengono mostrati come etichette colorate. La versione rilevata da F95Zone resta distinta dalla versione personale, che si può scrivere a mano o copiare con un pulsante dalla versione più recente.
- La scheda mostra le stelle personali e, quando presenti nella pagina, la valutazione F95Zone, i like e le visualizzazioni. Il pulsante **Modifica metadati** apre un editor separato.
- Le schede della libreria mostrano valutazione a stelle, like, visualizzazioni, stato, engine e data di aggiornamento del thread. La libreria si può ordinare per nome, data dell'ultimo aggiornamento, data d'inserimento, valutazione, like o visualizzazioni, invertendo la direzione.
- I commenti inseriti nell'app sono personali e locali; post e recensioni di F95Zone restano consultabili dal thread originale.
- Il parser legge i metadati HTML e la struttura XenForo del primo post, oltre alle date nei tag `time`/`abbr`; variazioni nella struttura di F95Zone possono richiedere aggiornamenti.
- Il servizio è pensato per uso personale e si lega alla porta `localhost` per impostazione predefinita. Non pubblicarlo direttamente su Internet.

## Configurazione

| Variabile | Default | Descrizione |
|---|---:|---|
| `SYNC_INTERVAL_SECONDS` | `21600` | Intervallo di sincronizzazione automatica |
