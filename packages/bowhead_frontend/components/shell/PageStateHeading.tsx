import type { CSSProperties, ReactNode } from "react";

const visuallyHidden: CSSProperties = {
  position: "absolute",
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: "hidden",
  clip: "rect(0, 0, 0, 0)",
  whiteSpace: "nowrap",
  border: 0,
};

export function PageStateHeading({ children }: { children: ReactNode }) {
  return <h1 style={visuallyHidden}>{children}</h1>;
}
