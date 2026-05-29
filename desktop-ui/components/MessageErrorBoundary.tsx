import React from "react";

interface State { hasError: boolean }

export class MessageErrorBoundary extends React.Component<
  { children: React.ReactNode },
  State
> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  render() {
    if (this.state.hasError) {
      return (
        <span className="text-red-400 text-xs italic">
          [Message could not be rendered]
        </span>
      );
    }
    return this.props.children;
  }
}
