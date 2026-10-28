# E-Mail senden

Sobald Ihr Team eine eigene Konfiguration unter **Team-Einstellungen → E-Mail-Dienst** gespeichert hat, können Sie Bowra im Chat bitten, eine reine Text-E-Mail zu senden. Jede E-Mail wird erst versendet, nachdem Sie sie selbst auf der Seite bestätigt haben. Der erste Teil dieses Leitfadens richtet sich an Mitglieder, die E-Mails senden. Der zweite Teil richtet sich an Teamverantwortliche und Teamadministratoren, die den E-Mail-Dienst einrichten.

## E-Mail aus einem Chat senden

Geben Sie im Chat Empfänger, Betreff und Inhalt an und bitten Sie Bowra, die E-Mail zu senden. Zum Beispiel: „Sende eine E-Mail an kunde@example.com mit dem Betreff Projektaktualisierung und folgendem Inhalt: …“ Sie können Bowra auch bitten, Analyseergebnisse aus dem aktuellen Chat in eine E-Mail umzuwandeln.

### Vor dem Senden bestätigen

Vor jedem Versand zeigt ein Fenster **E-Mail bestätigen** die vollständige Empfängerliste, den Betreff und den Inhalt. Prüfen Sie die Angaben und wählen Sie dann **Senden**. Wählen Sie **Nicht senden** oder tun Sie innerhalb der angezeigten Zeit nichts, wird die E-Mail nicht versendet; Bowra teilt Ihnen das im Chat mit. Eine versendete E-Mail kann nicht zurückgerufen werden.

Nur Sie, also die Person, die diesen Chat gestartet hat, können die E-Mail sehen und bestätigen. E-Mails werden niemals über geplante Aufgaben, Feishu oder andere Bereiche versendet, in denen Sie sie nicht auf der Webseite bestätigen können.

### Zahlen in der E-Mail

Beträge, Prozentsätze und andere Zahlen im Inhalt müssen aus Daten stammen, die im aktuellen Chat tatsächlich abgerufen wurden, oder aus Zahlen, die Sie selbst in Ihren Nachrichten eingegeben haben. Lässt sich eine Zahl im Entwurf von Bowra nicht in den Daten dieses Chats finden, erscheint kein Bestätigungsfenster. Bowra korrigiert zuerst die Zahlen und bittet Sie danach um Bestätigung.

Um Ergebnisse aus einem früheren Chat zu versenden, bitten Sie Bowra, die relevanten Daten im aktuellen Chat erneut abzufragen. Anschließend können Sie die E-Mail senden.

E-Mails enthalten ausschließlich reinen Text. Canvas, Berichte und andere Dateien werden nicht angehängt.

### Wenn kein E-Mail-Dienst eingerichtet ist

Hat Ihr Team keine eigene E-Mail-Dienstkonfiguration gespeichert, teilt Bowra Ihnen mit, dass der Dienst derzeit nicht verfügbar ist. Dasselbe gilt, wenn **Effektive Zustellkonfiguration** anzeigt, dass der Instanzstandard übernommen wird: Dieser Standard wird nur für System-E-Mails wie Einladungen, E-Mail-Verifizierung und Passwortzurücksetzungen verwendet, nicht für E-Mails aus Chats. Bitten Sie eine Teamverantwortliche oder einen Teamverantwortlichen oder die Administration, den Dienst wie unten beschrieben einzurichten.

## E-Mail-Dienst für das Team einrichten

Teamverantwortliche und Teamadministratoren können unter **Team-Einstellungen → E-Mail-Dienst** einen SMTP-Dienst für das Team einrichten. Danach werden Einladungen, E-Mail-Verifizierungen, Passwortzurücksetzungen und Benachrichtigungen zuerst über die eigene Konfiguration des Teams versendet. Mitglieder können außerdem wie oben beschrieben E-Mails aus Chats senden.

### Einrichtungsschritte

1. Öffnen Sie **Team-Einstellungen → E-Mail-Dienst**. Unter **Effektive Zustellkonfiguration** sehen Sie, ob das Team eine eigene Konfiguration hat, den Instanzstandard übernimmt oder noch keine Konfiguration besitzt.
2. Geben Sie unter **SMTP-Verbindung** den von Ihrem E-Mail-Anbieter genannten SMTP-Server, Port und Verschlüsselungstyp ein. Übliche Ports sind 465 (SSL/TLS) und 587 oder 2525 (STARTTLS).
3. Geben Sie unter **Authentifizierung** den SMTP-Benutzernamen und das Passwort oder App-Passwort ein. Die meisten E-Mail-Dienste erfordern ein eigenes SMTP- oder App-Passwort statt des Anmeldepassworts. Ein gespeichertes Passwort wird nie wieder angezeigt. Lassen Sie das Feld beim erneuten Speichern leer, wenn das vorhandene Passwort erhalten bleiben soll.
4. Geben Sie unter **Absenderidentität** eine von Ihrem E-Mail-Anbieter autorisierte Absenderadresse und den Anzeigenamen ein. Bowra hat keine Standard-Absenderdomain; ohne autorisierte Absenderadresse kann keine E-Mail zugestellt werden. Wählen Sie dann **Speichern**. Sie können auch **Speichern und testen** auswählen, um direkt zu speichern und eine Test-E-Mail zu senden.
5. Wählen Sie **Test-E-Mail senden**. Die Test-E-Mail geht an Ihr eigenes Postfach. Kommt sie an, funktioniert die Konfiguration. Andernfalls prüfen Sie auch den Spam-Ordner.

Benötigt das Team keine eigene Konfiguration mehr, wählen Sie **Konfiguration löschen**. Diese Option erscheint nur, wenn das Team eine eigene Konfiguration gespeichert hat. System-E-Mails verwenden danach gegebenenfalls den Instanzstandard. Das Senden von E-Mails aus Chats ist dann nicht verfügbar.

### Hinweise

- Die Cloud- (SaaS-) Edition kann nur E-Mail-Server verwenden, die aus dem öffentlichen Internet über Port 25, 465, 587 oder 2525 erreichbar sind. Geben Sie eine interne Firmenadresse oder einen anderen Port ein, meldet das Speichern, dass die Adresse durch die Sicherheitsrichtlinie blockiert oder der Port nicht unterstützt wird. Verwenden Sie stattdessen die öffentliche SMTP-Adresse Ihres E-Mail-Anbieters.
- Private Bereitstellungen können interne Firmen-Mailserver und andere Ports verwenden.
- Nur Teamverantwortliche und Teamadministratoren können die Konfiguration des E-Mail-Dienstes anzeigen und ändern. Andere Mitglieder sehen diesen Eintrag nicht.
