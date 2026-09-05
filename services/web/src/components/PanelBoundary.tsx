import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

interface Props {
  /** Shown to the reader when this panel fails, e.g. "Knowledge repair". */
  name: string;
  children: ReactNode;
}

/**
 * Keeps one panel's failure from taking down the reader.
 *
 * An uncaught error during render unmounts React's whole tree, so a panel mounted at App
 * level can blank the entire application — the chapter you were reading included. That is
 * a wildly disproportionate consequence for an auxiliary panel, and it is exactly what
 * happened when the repair panel read a field that a slightly older API response did not
 * have: reading the book became impossible because a status widget could not count.
 *
 * The reading experience does not depend on any of these panels, so none of them should be
 * able to remove it. This isolates the blast radius to the panel itself.
 */
export class PanelBoundary extends Component<Props, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Left in the console deliberately: the panel degrades quietly for the reader, but a
    // developer still needs the stack to find out why.
    console.error(`${this.props.name} panel failed`, error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <p className="chapter-list-error" role="alert">
          The {this.props.name} panel could not be displayed. Reading is unaffected; the
          details are in the browser console.
        </p>
      );
    }
    return this.props.children;
  }
}
