# Einen Agent mit Coach verbessern

Wenn ein Agent, dessen Owner du bist, immer wieder etwas falsch macht, sag es Coach in deinen eigenen Worten. Coach macht daraus einen Bewertungspunkt, prüft mit dir an einigen echten Antworten, ob er den Punkt so beurteilt wie du, ändert dann den Entwurf des Agents und berichtet, ob die Änderung geholfen hat. Du musst den Punkt nicht selbst schreiben; du sagst nur, ob Coach jede Antwort richtig beurteilt hat.

## Beginnen

1. Öffne eine Unterhaltung mit **@Coach** oder wähle bei einem Daumen runter eines Mitglieds in der ausstehenden Einträgen des Agents **Damit den Agent verbessern**.
2. Sag, was nicht stimmt, zum Beispiel: „Es sagt nie, welchen Zeitraum die Zahlen abdecken.“ Coach formuliert daraus einen Punkt und zeigt eine Prüfkarte.

Hat ein Mitglied ohne Kommentar mit Daumen runter bewertet, fragt Coach zuerst, was an der Antwort falsch ist. Hat der Agent noch keine Antworten, entwirft Coach den Punkt aus der Beschreibung des Agents; bestätige ihn wie gewohnt. Ein Punkt mit einem Tool zählt, sobald du ihn bestätigst; ein so entworfener Beurteilungs- oder Datenpunkt dient nur zur Information, bis er an echten Antworten geprüft wurde.

## Eigene Testfälle verwalten

Gruppenergebnisse zählen Starts über **Diese Testgruppe ausführen** oder **Speichern und ausführen**. **Diese Änderung testen** vergleicht Versionen; die Ergebnisse bleiben auf **Veröffentlichen**. Frühere Antworten behalten ihre damaligen Fragen und Erwartungen, auch wenn du die Fälle später bearbeitest.

Auf **Bewerten** bearbeitest du gespeicherte Fragen über **Frage bearbeiten**. **Speichern und ausführen** speichert die aktuellen Fälle und führt genau diese Version aus. **Tests speichern** speichert ohne Ausführung. Schlägt das Speichern fehl, startet nichts. Ist der Start nach dem Speichern unbestätigt, stellt ein erneuter Start dieselbe Anfrage wieder her. Hat sich die Agent-Version geändert, prüfe zuerst das aktualisierte Ziel.

**Ausführung bestanden** bestätigt die Ausführungsprüfungen. Die **Antwortkriterien** werden separat angezeigt: fehlende oder teilweise Bewertungen sind ausdrücklich gekennzeichnet; Modellbewertungen dienen als Hinweis. Über **Antworten und Kriterien anzeigen** und einen aufgeklappten Fall siehst du die damalige Frage, Antwort und Erwartungen, auch bei bestandenen Fällen. Eine abweichende Bewertungsprüfung beweist keine falsche Agent-Antwort.

Automatische Wiederholungen sind unter den Ergebnissen eingeklappt. **Ausgangsbasis mit Coach prüfen** nutzt das bestehende Gespräch zur Antwortprüfung; eine tägliche Prüfung ist nicht erforderlich.

Unter **Bewerten** können Sie jeden Fall entfernen, auch den letzten, und anschließend die Tests speichern. Ohne Fälle wird Ihre eigene Testsammlung aus künftigen Tests entfernt; frühere Ergebnisse bleiben im Verlauf. Sie können jederzeit neue Fälle hinzufügen. Eine entfernte Testsammlung kann nicht einzeln ausgeführt werden. Der Test der Änderung führt weiterhin die grundlegende Sicherheitsprüfung aus.

Fehlende eigene Testfälle verhindern die Veröffentlichung nicht und erfordern keine Begründung. Die Seite und die Prüfung freigegebener Agents weisen darauf hin, dass die Antwortqualität nicht bewertet wurde. Andere Sicherheitsprüfungen, tatsächliche Testfehler und die Prüfung freigegebener Agents gelten weiterhin.

## Coachs Urteile prüfen

Die Prüfkarte listet einige Antworten. Jede Zeile zeigt die Frage, einen Ausschnitt der Antwort mit dem hervorgehobenen Satz, auf den sich Coach stützt, und Coachs Schluss.

- Lass eine Zeile unverändert, wenn Coach richtig liegt.
- Wähle **Falsch**, wenn Coachs Urteil falsch ist, oder **Unsicher**, wenn du es nicht sagen kannst.
- Wähle dann **Der Rest ist richtig**. Sind alle Urteile richtig, ist das der einzige Klick.

Eine Zeile jeder Karte ist eine Antwort, aus der Coach das Verlangte entfernt hat; sie ist mit **Von Coach bearbeitet** markiert. Beurteile sie wie die anderen.

Danach gilt:

- **Alles richtig:** Der Punkt wird bestätigt und in die Testsuite des Agents aufgenommen; Coach beginnt, den Entwurf zu ändern.
- **Eine Zeile Falsch:** Coach schlägt eine klarere Formulierung vor und fragt nach weiteren Antworten. Die neue Karte zeigt alte und neue Formulierung; mit **Original behalten, 2 weitere prüfen** behältst du deine.
- **Unsicher:** Coach ersetzt die Antwort durch eine andere. Nach drei Mal bittet Coach dich, den Punkt anders zu formulieren.
- Coach fragt manchmal nach einer einzelnen Zeile noch einmal; wähle dort deine Antwort und **Speichern**.

Nach dem Absenden schickt die Karte Coach eine kurze **@Coach**-Nachricht mit dem Ergebnis, damit Coach in derselben Unterhaltung weitermacht. Die eingeklappte Karte sagt dann, wo der Punkt steht und was als Nächstes passiert, zum Beispiel **Punkt bestätigt. Als Nächstes: Coach schlägt daraus Testfälle vor**. Schreibst du gerade eine eigene Nachricht, bleibt dein Entwurf erhalten und die Karte bietet **An Coach senden** an, um diese Nachricht zu senden, sobald du so weit bist. Später in der Unterhaltung kannst du auch einfach **@Coach** schreiben; Coach setzt die offene Prüfung fort.

Um den Punkt selbst umzuformulieren, wähle **Punkt umformulieren**. Die Karte beginnt neu; in die Suite wird erst bei der Bestätigung geschrieben. Sagt die Karte, sie sei veraltet, wurde der Punkt anderswo geändert: Wähle **Neu prüfen**.

Nur der Owner des Agents kann eine Prüfkarte beantworten. Andere sehen eine entsprechende Zeile.

## Das Ergebnis lesen

Nachdem Coach den Entwurf geändert hat und eine Prüfung gelaufen ist, zeigt Coach eine Ergebniskarte:

- **Dein Anliegen:** wie viele Testfälle mit deinem Punkt ihn vor und nach der Änderung erfüllt haben.
- **Fragen, die Coach nicht kennt:** die zurückgehaltenen Testfälle (siehe unten), vorher und nachher.
- **Schlechter geworden:** andere Testfälle, die vorher bestanden und jetzt nicht. Mit **Den verschlechterten beheben** bittest du Coach, das zu beheben.
- Zwei neue Antworten zum Lesen.

Bei weniger als drei Testfällen zeigt die Karte Anzahlen statt Prozentwerte. Hat sich das Bewertungsmodell des Teams während der Prüfung geändert, zählt das Ergebnis nicht; wähle **Mit aktuellem Modell erneut ausführen** und lass Coach neu prüfen.

## Zurückgehaltene Testfälle

Wenn Coach für einen Punkt Testfälle hinzufügt, wird einer davon zurückgehalten. Coach sieht weder Frage noch Antworten dieses Testfalls, sondern erfährt nur, ob er bestanden hat. Besteht eine Änderung auch ihn, ist der Agent wirklich besser geworden und nicht nur bei den Fragen, die Coach kannte.

Auf dem Tab **Evaluieren** hat jeder Testfall einen Schalter **Zurückgehalten**. Du kannst:

- Ihn für einen anderen Testfall desselben Punkts einschalten; die Markierung wandert dorthin.
- Ihn ausschalten. Ist es der einzige zurückgehaltene Testfall des Punkts, wirst du um Bestätigung gebeten, und spätere Berichte sagen, dass die Änderung für diesen Punkt nicht unabhängig geprüft werden konnte.

Nur du als Owner kannst zurückgehaltene Testfälle ändern, Coach nicht. Änderungen gelten, sobald du die Suite speicherst. Release-Prüfungen laufen immer über alle Testfälle.

Release-Prüfungen verwenden immer die zuletzt gespeicherte Fassung jeder aktiven Suite: Sobald du eine Suite speicherst, einen Punkt bestätigst oder vorgeschlagene Testfälle übernimmst, wird der nächste vorbereitete Kandidat damit geprüft. Ein bereits vorbereiteter Kandidat behält die Testfälle, mit denen er vorbereitet wurde. Wurden danach Testfälle gespeichert, weist der Tab **Veröffentlichen** darauf hin, und der nächste Schritt ist der Vergleich: **Versionsvergleich ausführen** aktualisiert zuerst den Testplan mit den zuletzt gespeicherten Testfällen und führt ihn dann aus, denn eine Veröffentlichung braucht Ergebnisse, die sie enthalten. Frühere Ergebnisse bleiben im Verlauf. Der Plan wird nur aktualisiert, solange kein Vergleich läuft und der Kandidat noch nicht zur Prüfung eingereicht ist. Ein bereits eingereichter Kandidat behält den eingereichten Plan und kann weiterhin genehmigt werden; während der Prüfung gespeicherte Testfälle gelten ab dem nächsten Kandidaten. Sollen sie vorher geprüft werden, ziehe den Kandidaten zurück und bereite ihn erneut vor.

## Herkunft der Punkte und Coachs Hinweise

Auf dem Tab **Evaluieren** zeigt ein mit Coach bestätigter Punkt die Worte, aus denen er entstand, und wie viele Antworten du geprüft hast, mit **Ansehen** für Details. Ein Punkt aus dem Daumen runter eines Mitglieds zeigt **Aus Mitgliederfeedback**; der Kommentar des Mitglieds erscheint dort nicht.

Wenn du einen Punkt hinzufügst oder änderst, liest Coach ihn und fügt nur bei einem Problem eine Zeile darunter hinzu: zu vage, zwei Dinge in einem Punkt, eine Zahl aus einer einzelnen Antwort oder gleich wie bzw. im Widerspruch zu einem anderen Punkt. Wähle **Diese Umformulierung verwenden**, **In zwei teilen** für einen Punkt, der zwei Dinge prüft, oder **So lassen**; für diese Formulierung erwähnt Coach es nicht mehr. Die Suite kann auch **Coach hat diese Suite geprüft** mit fehlender Abdeckung und doppelten Testfällen zeigen; **Nebeneinander ansehen** zeigt beide Testfälle, damit du beide behältst oder einen entfernst, was beim Speichern der Suite wirksam wird.

Das **⋯**-Menü eines Punkts enthält **An Antworten prüfen**, womit Coach den Punkt mit dir an echten Antworten prüft, und bei einem gespeicherten Punkt **Kalibrierung ansehen** mit den Beispielen, wie jedes beurteilt werden sollte und wurde, und dem verwendeten Bewertungsmodell. All das sind Hinweise: Sie blockieren weder Speichern noch Release, und ist Coach nicht verfügbar, erscheint nichts.
