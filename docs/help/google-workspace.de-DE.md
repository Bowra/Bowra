# Google Workspace

Mit verbundenem Google Workspace können Agents die Google-Dokumente, -Tabellen und -Präsentationen lesen, die Sie freigeben, Ihren Kalender ansehen und Ergebnisse auf Ihre ausdrückliche Bitte direkt zurück in Google schreiben (ohne zusätzliche Bestätigung) – immer mit Ihrem eigenen Google-Zugriff. Jedes Mitglied nutzt nur seine eigene Autorisierung; niemand sonst, auch keine Team-Admins, kann Ihre Google-Daten lesen oder verwenden.

## Admins: Team-Verbindung einrichten

Nur Team-Inhaber und Team-Admins können die Verbindung konfigurieren. Sie verwendet die eigene Google-Cloud-App Ihres Unternehmens; Bowra stellt keine gemeinsame App bereit.

1. Erstellen Sie in der Google Cloud Console einen OAuth-Client vom Typ „Webanwendung" mit dem Nutzertyp „Intern" (nur Ihre Organisation).
2. Öffnen Sie **Team-Einstellungen → Tools → Integrierte Drittanbieter-Tools** und wählen Sie Google Workspace. Übernehmen Sie den angezeigten **Autorisierten Weiterleitungs-URI** in „Autorisierte Weiterleitungs-URIs" und den **Autorisierten JavaScript-Ursprung** in „Autorisierte JavaScript-Ursprünge". Fordert die Seite zuerst eine Zugriffsadresse an, registrieren Sie unter **Team-Einstellungen → Öffentlicher Zugang** eine HTTPS-Adresse.
3. Deklarieren Sie die aufgeführten Bereiche auf dem OAuth-Zustimmungsbildschirm.
4. Geben Sie in Bowra Client-ID, Client-Secret und Ihre Google-Workspace-Domain ein und aktivieren Sie die benötigten Funktionen:
   - Basis (erforderlich): von Mitgliedern in Bowra ausgewählte Dateien lesen und schreiben; neue Dokumente, Präsentationen und Tabellen erstellen;
   - Per Link lesen: jedes verlinkte Dokument, jede Tabelle und Präsentation lesen;
   - Vorhandene Dokumente und Präsentationen bearbeiten, In vorhandene Tabellen schreiben: vorhandene Dateien ändern, die das Mitglied bearbeiten darf;
   - Kalender: die eigenen Kalendertermine des Mitglieds ansehen (standardmäßig aus).
5. Um „Aus Google Drive auswählen" in Chats und in der Wissensdatenbank zu nutzen, erstellen Sie in Google Cloud einen Browser-API-Schlüssel, dessen Website-Einschränkungen Ihre Team-Adresse und `https://docs.google.com/*` enthalten, und tragen Sie API-Schlüssel und Projektnummer in Bowra ein.
6. Klicken Sie auf **Mit meinem Google-Konto autorisieren**, führen Sie mit Ihrem eigenen Firmenkonto eine Autorisierung durch, um die Einrichtung zu bestätigen, und aktivieren Sie dann die Verbindung.

Die Karte zeigt, wie viele Mitglieder autorisiert haben. Eine Änderung von Client-ID oder Client-Secret widerruft die Google-Autorisierung aller Mitglieder; alle müssen sich neu verbinden. Bowra fordert nie an, E-Mails zu lesen oder das gesamte Drive zu durchsuchen.

Setzt Ihr eigener Reverse-Proxy oder Ihr Sicherheits-Gateway eine Content Security Policy, erlauben Sie `accounts.google.com`, `apis.google.com` und `docs.google.com`; andernfalls lässt sich „Aus Google Drive auswählen" nicht öffnen.

## Mitglieder: Google-Konto verbinden

Sobald Ihr Team Google aktiviert hat, klicken Sie unter **Meine Verbindungen** auf **Google verbinden** und autorisieren Sie mit dem Firmen-Google-Konto, das zu Ihrer Bowra-Anmelde-E-Mail passt. Die Autorisierung wird abgelehnt, wenn die E-Mail-Adressen abweichen oder das Konto nicht zu Ihrer Firmendomain gehört.

Sie können die Verbindung jederzeit unter „Meine Verbindungen" trennen. Läuft Ihre Autorisierung ab oder aktiviert ein Admin eine neue Funktion, fordert die Karte Sie zum erneuten Verbinden oder Aktualisieren auf.

## Im Chat verwenden

- Dateien lesen: Fügen Sie einen Link zu einem Google-Dokument, einer Tabelle oder Präsentation ein, oder wählen Sie im **+**-Menü des Eingabefelds (auf schmalen Bildschirmen unter **Weitere Tools**) die Option **Aus Google Drive auswählen**; ausgewählte Dateien werden als Zeilen „Name Link" an der Cursorposition eingefügt. Der Agent liest mit Ihrem Zugriff; lange Dateien werden abschnittsweise gelesen.
- Kalender ansehen: Fragen Sie einfach, zum Beispiel „Welche Termine habe ich nächste Woche?". Ihr Team muss die Kalender-Funktion aktiviert haben.
- Zurück in Google schreiben: Bitten Sie ausdrücklich darum, zum Beispiel „Schreib diese Analyse als Google-Dokument" oder „Hänge die Ergebnisse an diese Tabelle an". Der Agent schreibt direkt; ist unklar, welche Datei oder welcher Abschnitt geändert werden soll, oder enthält der Zielbereich einer Tabelle bereits Daten, fragt er Sie zuerst. Geschrieben wird nur in Web-Chats. Nach dem Schreiben erscheint rechts im Canvas eine Karte zur Datei mit dem Zeitpunkt des Schreibens: Standardmäßig zeigt sie eine Momentaufnahme des Inhalts zu diesem Zeitpunkt. Mit dem Umschalter **Momentaufnahme** / **Live-Vorschau** auf der Karte sehen Sie die aktuelle Google-Ansicht (Ihr Browser muss beim passenden Google-Konto angemeldet sein, und manche Browser können sie nicht anzeigen; eine Zeile unter der Vorschau erklärt immer, dass Google das Einbetten vielleicht nicht zulässt oder eine Anmeldung verlangt, wenn nichts erscheint – wechseln Sie dann zurück zur Momentaufnahme oder öffnen Sie die Datei in Google), oder Sie wählen **In Google öffnen**, um die Datei in einem neuen Tab zu öffnen.

Vor dem Schreiben prüft Bowra, ob die Zahlen aus den in dieser Runde abgerufenen Daten stammen, und schreibt nicht, wenn das nicht der Fall ist. Wurde eine Schreibanfrage gesendet, das Ergebnis ist aber unbekannt, weist die Antwort Sie an, in Google nachzusehen; es wird nichts automatisch wiederholt. Fehler lassen sich über den Versionsverlauf in Google Docs oder Präsentationen rückgängig machen.

## In die Wissensdatenbank importieren

Öffnen Sie in der Wissensdatenbank den Hochladen-Dialog, klicken Sie unter der Ablagefläche auf **oder Aus Google Drive importieren** und wählen Sie Dateien aus. Pro Import werden höchstens 20 Dateien übernommen; importieren Sie weitere erneut. Google-Dokumente, -Tabellen und -Präsentationen werden in die passenden Office-Dateien umgewandelt; Dateien über 10 MB können nicht aus Google exportiert werden – teilen oder speichern Sie sie vorher separat. Importierte Dateien werden nicht mit dem Original synchronisiert; importieren Sie nach Änderungen erneut.

## Häufige Fragen

- Keine Schaltfläche „Google verbinden": Ihr Team hat Google noch nicht aktiviert. Wenden Sie sich an einen Team-Admin.
- Eine Datei lässt sich nicht öffnen: Möglicherweise haben Sie keinen Zugriff darauf, oder Ihr Team hat „Per Link lesen" nicht aktiviert. Wählen Sie die Datei stattdessen über **Aus Google Drive auswählen** aus.
- „Aus Google Drive auswählen" öffnet sich nicht: Öffnen Sie Bowra über die registrierte Adresse Ihres Teams, oder bitten Sie einen Admin, die Website-Einschränkungen des API-Schlüssels zu prüfen.
