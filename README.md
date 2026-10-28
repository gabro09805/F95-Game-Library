# F95 Library

Libreria web self-hosted per organizzare i thread di giochi F95Zone. La prima versione importa titolo, copertina, galleria immagini, overview, versione corrente e statistiche del thread quando sono disponibili; consente ricerca, stato personale, commenti e controllo periodico delle modifiche ai thread.

Non scarica, installa o avvia giochi. Non è affiliata a F95Zone.

## Avvio con Docker

1. Installa Docker Desktop.
2. Controlla la variabile `APP_PORT` nel file `.env` (predefinita a `8080`).
3. Nella cartella del progetto, avvia `docker compose up --build -d`.
4. Sul computer che ospita Docker apri `http://localhost:8080`; da un altro computer della rete apri `http://IP_DEL_SERVER:8080`, sostituendo `8080` con il valore di `APP_PORT`.

La porta è pubblicata su tutte le interfacce di rete del computer host, così gli altri dispositivi della LAN possono collegarsi. Se necessario, consenti la porta scelta nel firewall di Windows. L'app non ha un login web: rendila accessibile solo su una rete fidata e non inoltrare la porta verso Internet.

Per cambiare porta, modifica `APP_PORT` in `.env` e ricrea il servizio con `docker compose up -d`. I dati sono conservati nel volume Docker `f95-library-data`; `docker compose down` non lo elimina.

Per una verifica manuale dei log: `docker compose logs -f`.

## Modalità sviluppo (hot reload)

Per sviluppare senza ricreare l'immagine a ogni modifica, avvia Docker Desktop e usa:

```powershell
docker compose -f compose.yaml -f compose.dev.yaml up -d
```

Il sorgente viene montato nel container: modifiche a HTML/CSS/JavaScript sono ricaricate dal browser automaticamente; modifiche ai file Python riavviano il server. Apri `http://localhost:<APP_PORT>` oppure, da un altro computer, `http://IP_DEL_SERVER:<APP_PORT>`. Per tornare alla modalità standard: `docker compose down`, poi `docker compose up -d --build`.

In alternativa, fuori da Docker, esegui `python dev.py` e apri lo stesso indirizzo locale della modalità server.

## Accesso a F95Zone

Apri **Impostazioni** (icona ⚙) e accedi con il tuo account F95Zone. L'app inoltra la password a F95Zone via HTTPS per il login e non la salva. Vengono memorizzati nel volume locale solo i cookie della sessione; la funzione **Disconnetti** li rimuove. La sessione serve per leggere i metadati dei thread che richiedono l'accesso, non per scaricare giochi.

I cookie di sessione sono credenziali temporanee: l'app li conserva nel database SQLite del volume `/data` con permessi limitati e li invia solo a F95Zone. Proteggi il volume e non condividerlo. Il servizio non ha un login web: qualsiasi dispositivo che raggiunge la porta può usare la libreria e le impostazioni. Limita la pubblicazione alla rete locale fidata e non esporlo direttamente su Internet. Il login F95Zone serve solo per leggere i contenuti autenticati; non protegge l'interfaccia dell'app. Il login F95Zone potrebbe richiedere passaggi aggiuntivi (per esempio CAPTCHA o verifica a due fattori) non gestiti dall'MVP.

In **Impostazioni** puoi scegliere il tema chiaro o scuro e la frequenza di controllo: manuale, ogni numero di ore, giorni o mesi. Il valore predefinito è ogni 6 ore; un mese corrisponde a 30 giorni. In modalità manuale puoi avviare il controllo con il pulsante ⟳. Le preferenze di aggiornamento sono conservate nel database locale e il tema nel browser. Se F95Zone rifiuta la sessione, l'errore è visibile nella scheda: accedi di nuovo da **Impostazioni**.

Per i dettagli degli aggiornamenti e gli eventuali contenuti completi, apri il thread originale da una scheda gioco. Il rilevamento automatico segnala un update quando riconosce una data di post più recente nella pagina.

## Dati e limiti dell'MVP

- Database SQLite nel volume `/data`.
- Importazione tramite URL del thread.
- Copertina, galleria e overview vengono estratte dal primo post del thread. L'overview si interrompe prima dei campi metadata successivi (come "Thread Updated"). Le immagini ospitate sui domini F95Zone passano da un proxy locale che invia il Referer richiesto e limita host, tipo e dimensione della risposta.
- Stato ed engine vengono mostrati come etichette colorate. La versione rilevata da F95Zone resta distinta dalla versione personale, che si può scrivere a mano o copiare con un pulsante dalla versione più recente.
- La scheda mostra le stelle personali e, quando presenti nella pagina, la valutazione F95Zone, i like e le visualizzazioni. Il pulsante **Modifica metadati** apre un editor separato.
- Le schede della libreria mostrano valutazione a stelle, like, visualizzazioni, stato, engine e data di aggiornamento del thread. La libreria si può ordinare per nome, data dell'ultimo aggiornamento, data d'inserimento, valutazione, like o visualizzazioni, invertendo la direzione. La scelta del campo e della direzione viene ricordata nel browser.
- I commenti inseriti nell'app sono personali e locali; post e recensioni di F95Zone restano consultabili dal thread originale.
- Il parser legge i metadati HTML e la struttura XenForo del primo post, oltre alle date nei tag `time`/`abbr`; variazioni nella struttura di F95Zone possono richiedere aggiornamenti.
- Il servizio è pensato per uso personale e si lega alla porta `localhost` per impostazione predefinita. Non pubblicarlo direttamente su Internet.

## Configurazione

| Variabile | Default | Descrizione |
|---|---:|---|
| `APP_PORT` | `8080` | Porta pubblicata sul computer host; è possibile modificarla in `.env` |
