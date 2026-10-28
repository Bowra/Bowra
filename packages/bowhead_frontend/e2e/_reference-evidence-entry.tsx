import { useState } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { ReferenceFollowUpQueue } from "../components/chat/reference-follow-up-queue";
import zh from "../messages/zh-CN.json";
import en from "../messages/en-US.json";

function Harness() {
  const [actor, setActor] = useState("user-1:team-1:generation-1");
  const locale = new URLSearchParams(location.search).get("locale") === "en-US" ? "en-US" : "zh-CN";
  return <NextIntlClientProvider locale={locale} messages={locale === "en-US" ? en : zh}>
    <main><h1>Queued reference source browser check</h1>
      <button onClick={() => setActor("user-2:team-1:generation-2")}>Switch User</button>
      <ReferenceFollowUpQueue workspaceId="ws_browser" sessionId="session-browser" actorScopeKey={actor} refreshKey="queue" canCancel={false} />
    </main>
  </NextIntlClientProvider>;
}
createRoot(document.getElementById("root")!).render(<Harness />);
