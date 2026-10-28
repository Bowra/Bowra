"use client";

import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  fallback: ReactNode;
  onError?: (err: Error) => void;
}
interface State { hasError: boolean }

/** Simple ErrorBoundary — fall back when chart rendering fails (usually to ChartTable). */
export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false };
  }
  static getDerivedStateFromError(_err: Error) {
    return { hasError: true };
  }
  componentDidCatch(err: Error, info: unknown) {
    console.error("[ChartBlock] render failed, falling back:", err, info);
    this.props.onError?.(err);
  }
  render() {
    if (this.state.hasError) return this.props.fallback;
    return this.props.children;
  }
}
