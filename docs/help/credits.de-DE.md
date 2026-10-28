# Kontingent prüfen und zuweisen

Community benötigt keine kommerzielle Aktivierung und hat keine feste Nutzerobergrenze. Credits bleiben von der Administration verwaltete Nutzungsbudgets; Modell- und Infrastrukturkosten trägt der Betreiber separat. Ein Planname bezeichnet hier eine Nutzungsstufe, kein gekauftes Community-Abonnement.

Nutzen Sie diese Anleitung, wenn Ihr Kontingent aufgebraucht ist oder Sie einem Mitglied Kontingent zuweisen müssen. Teamverantwortliche und Teamadministratoren können Teamkontingent zuweisen.

## So funktioniert das Kontingent

- Analysen verbrauchen Kontingent für Nachrichten, zusätzliche Rückfragen, Fragen über verbundene Kanäle, Flow-Testläufe und geplante Aufgaben.
- Abgerechnet werden die Inhalte, die in einem Durchlauf tatsächlich verarbeitet wurden. Die Belastung erfolgt nach Abschluss des Durchlaufs.
- Neue Arbeiten erfassen Eingabe, Cache-Treffer und Ausgabe für jedes Modell getrennt. Die Mengen werden mit den beim Modellaufruf erfassten Faktoren multipliziert, zusammengezählt, durch 1.000 geteilt und einmal aufgerundet. Alle drei Faktoren sind standardmäßig 1; ältere Buchungen behalten ihre bisherige Berechnung. Meldet ein Anbieter Cache-Treffer getrennt von der Eingabe, kann sich der Betrag auch bei drei Faktoren von 1 ändern.
- Für eine neue Analyse benötigt eine Person sowohl persönliches Kontingent als auch verfügbares Kontingent im Teampool.
- Monatliche Kontingente werden entsprechend dem Tarif zurückgesetzt. Nicht verbrauchtes Kontingent wird nicht übertragen.

## Aufgebrauchtes Kontingent prüfen und beheben

Die Anzeige oben rechts zeigt Ihr verbleibendes Kontingent. Unter 20 % des Kontingents ändert sie die Farbe und zeigt den verbleibenden Anteil, unter 5 % erscheint sie in der Warnfarbe mit dem Hinweis, dass das Kontingent fast aufgebraucht ist. Wählen Sie die Anzeige aus (oder drücken Sie die Eingabetaste, wenn sie den Fokus hat), um eine Übersicht zu öffnen: Ihr Plan, ein Balken mit dem bereits verbrauchten Anteil des Kontingents für diesen Zeitraum, das Datum, an dem das Kontingent zurückgesetzt wird, und ein Hinweis, wenn das Kontingent knapp wird. Mit Esc oder einem Klick außerhalb schließen Sie sie wieder. Auf dem Smartphone oder in einem schmalen Fenster befindet sich die Anzeige unten im Navigationsmenü, und die Übersicht öffnet sich am unteren Bildschirmrand. Wenn Ihre Rolle das Teamkontingent einsehen darf, enthält die Übersicht außerdem einen Link zu Ihren eigenen Verbrauchsdetails. Unter **Persönliche Einstellungen → Konto** sehen Sie den Monatsstand und das Kontingent. Wenn es aufgebraucht ist, können Sie keine neue Arbeit beginnen. In einem bereits laufenden Gespräch halten die Agents beim nächsten Schritt an, berichten, was fertig ist und was noch fehlt, und Sie können nach dem Aufladen fortfahren; der Verbrauch bis dahin kann den angezeigten Stand leicht negativ machen. Nachdem Kontingent zugewiesen wurde, wiederholen Sie dieselbe Rückfrage oder den fehlgeschlagenen Folgeschritt. Sie müssen die Frage nicht neu erstellen.

Bitten Sie eine teamverantwortliche oder administrierende Person um Kontingent.

## Einem Mitglied Kontingent zuweisen

Für Teamverantwortliche und Teamadministratoren:

1. Öffnen Sie **Administrationskonsole → Kontingent und Abrechnung**.
2. Suchen Sie das Mitglied unter **Benutzer** und wählen Sie **Kontingent zuweisen**.
3. Geben Sie den Betrag ein, notieren Sie optional einen Grund und bestätigen Sie. Der Betrag wird vom Teampool abgezogen.
4. Reicht der Teampool nicht aus, wählen Sie **Teamkontingent anpassen**, legen Sie den neuen Umfang und einen Grund fest und kehren Sie anschließend zur Zuweisung zurück.

Sie können das Kontingent, den Tarif oder den Saldo eines Mitglieds auch direkt ändern. Prüfen Sie, ob **Saldo / Kontingent** aktualisiert wurde. Ein negativer Saldo wird zuerst ausgeglichen. Ein Teampool mit Stand null verhindert neue Analysen für alle, auch wenn einzelne Mitglieder noch einen positiven Saldo haben.

In den Verbrauchsdetails eines Mitglieds zeigt die Modellspalte die für neue Belastungen erfassten Modelle. Klappen Sie einen Eintrag auf, um die drei Tokenmengen und ihre Faktoren zu sehen; mehrere Modelle werden einzeln aufgeführt. Verbrauch ohne bestätigten Modellaufruf erscheint als unbekanntes Modell mit Faktoren von 1. Bei älteren Einträgen kann das Modell fehlen. Der CSV-Export des Mitglieds enthält ebenfalls Modellnamen und erfasste Modellmengen.

Weitere Informationen: [Teamrollen und Berechtigungen](permissions/team-roles.de-DE.md).
