import "./_process-shim";
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { useStickToBottom } from "../lib/use-stick-to-bottom";

/**
 * A finished conversation in the real chat scroll container (`.v2-messages`, smooth scrolling from the
 * app stylesheet). "Open Results" narrows the column the way the split pane does, while a sidebar beside
 * it animates its width for 200 ms, so the column reflows over several frames.
 */
const SENTENCE = "门店销售额在第一季度整体上升，三月的增长主要来自华东区域的新开门店与会员复购。";
const TURNS = Array.from({ length: 12 }, (_, turn) => ({
  token: `t_${turn}`,
  paragraphs: Array.from({ length: 5 }, (_, index) => SENTENCE.repeat(2 + ((turn + index) % 4))),
}));

function Conversation() {
  const stick = useStickToBottom(false);
  const [resultsOpen, setResultsOpen] = useState(false);
  return (
    <div style={{ display: "flex", height: "100vh" }}>
      <nav style={{ width: resultsOpen ? 56 : 240, transition: "width 200ms", flex: "none", background: "#eee" }} />
      <div style={{ width: resultsOpen ? 560 : undefined, flex: resultsOpen ? "none" : 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        <button id="toggle-results" type="button" onClick={() => setResultsOpen((open) => !open)}>Results</button>
        <div className="v2-messages" ref={stick.scrollRef} tabIndex={-1} style={{ flex: 1 }}>
          <div className="v2-messages-inner" ref={stick.contentRef}>
            {TURNS.map((turn) => (
              <section key={turn.token} data-results-message-token={turn.token}>
                <h3>{turn.token}</h3>
                {turn.paragraphs.map((text, index) => <p key={index} id={`${turn.token}-p${index}`}>{text}</p>)}
              </section>
            ))}
          </div>
        </div>
      </div>
      {resultsOpen ? <aside style={{ flex: 1, background: "#f6f6f6" }} /> : null}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<Conversation />);
