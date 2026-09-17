#!/usr/bin/env bash
# Setzt das Repo nach einem Secure-Coder-Lauf in den Demo-Ausgangszustand zurueck.
#
# Was Secure Coder tut: er klont origin/main, legt einen EIGENEN Branch an, commitet
# den Fix (log4j 2.14.1 -> gepatcht) und oeffnet einen Pull Request. `main` bleibt
# dabei unberuehrt -- solange niemand den PR merged.
#
# Daraus folgt die wichtigste Demo-Regel:
#   >>> Den PR NIE mergen. <<<
#   Dann ist das Zuruecksetzen ein Zweizeiler und `main` bleibt sauber.
#
# Aufruf:
#   ./scripts/reset-securecoder-demo.sh            # nur anzeigen, nichts aendern
#   ./scripts/reset-securecoder-demo.sh --apply    # PRs schliessen, Branches loeschen
#
# Voraussetzung: `gh` angemeldet mit Zugriff auf das Repo.
set -uo pipefail

REPO="${REPO:-test-mabu/techexchange-demo-app}"
MAIN="${MAIN:-main}"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

say() { printf '%s\n' "$*"; }
run() {
  if [ "$APPLY" = "1" ]; then "$@"; else say "    [dry-run] $*"; fi
}

say "Repo: $REPO"
say "Modus: $([ "$APPLY" = 1 ] && echo 'AENDERND (--apply)' || echo 'nur anzeigen')"
say ""

# ---------- 1. Pull Requests ----------
say "1. Offene Pull Requests"
PRS=$(gh pr list --repo "$REPO" --state open --json number,headRefName,title \
      --jq '.[] | "\(.number)\t\(.headRefName)\t\(.title)"' 2>/dev/null)
if [ -z "$PRS" ]; then
  say "   keine offenen PRs"
else
  printf '%s\n' "$PRS" | while IFS=$'\t' read -r num branch title; do
    say "   PR #$num  ($branch)  $title"
    run gh pr close "$num" --repo "$REPO" --delete-branch \
        --comment "Demo-Reset: geschlossen ohne Merge."
  done
fi
say ""

# ---------- 2. Uebriggebliebene Branches ----------
# --delete-branch oben raeumt den Branch meist schon weg; das hier faengt Reste
# und Branches ohne PR ab.
say "2. Verwaiste Remote-Branches (alles ausser $MAIN)"
BRANCHES=$(git ls-remote --heads origin 2>/dev/null \
           | sed 's#.*refs/heads/##' | grep -v "^${MAIN}$")
if [ -z "$BRANCHES" ]; then
  say "   keine -- nur $MAIN vorhanden"
else
  for b in $BRANCHES; do
    say "   $b"
    run git push origin --delete "$b"
  done
fi
say ""

# ---------- 3. Der eigentliche Pruefpunkt ----------
say "3. Ist die Schwachstelle auf $MAIN noch da?"
git fetch origin "$MAIN" --quiet 2>/dev/null
VER=$(git show "origin/${MAIN}:java-app/pom.xml" 2>/dev/null \
      | sed -n 's#.*<log4j.version>\(.*\)</log4j.version>.*#\1#p' | head -1)
if [ "$VER" = "2.14.1" ]; then
  say "   OK -- log4j.version = $VER auf origin/$MAIN (CVE-2021-44228 vorhanden)"
else
  say "   ACHTUNG -- log4j.version = '${VER:-unbekannt}', erwartet 2.14.1."
  say "   Der Fix ist offenbar auf $MAIN gelandet (PR wurde gemerged)."
  say "   Zuruecknehmen, ohne Historie zu faelschen:"
  say "       git revert -m 1 <merge-commit>   # bei einem Merge-Commit"
  say "       git revert <commit>              # bei Squash/Rebase"
  say "       git push origin $MAIN"
  say "   Oder, falls ein Anker-Tag existiert:"
  say "       git push origin +demo-baseline:$MAIN    # ueberschreibt $MAIN, destruktiv"
fi
say ""
say "Danach in Concert:"
say "  * Secure Coder oeffnen -- pro Aufruf startet ohnehin eine neue Sitzung."
say "  * Die Action zu CVE-2021-44228 muss wieder offen sein. Steht sie auf erledigt,"
say "    die CVE in der Vulnerability-Dimension erneut priorisieren."
say "  * Nur noetig, wenn nach dem Fix neu gescannt wurde: den code_scan mit der"
say "    verwundbaren pom.xml erneut hochladen (neue serialNumber, sonst"
say "    'cyclonedxSbomExist'). Siehe scripts/scan-and-upload.sh"
