import React from 'react';
import { ErrorState } from '@/components/shared/ErrorState';

interface ErrorBoundaryProps {
  children: React.ReactNode;
  fallback?: React.ReactNode | ((resetErrorBoundary: () => void) => React.ReactNode);
  // Issue #779 (UI-R2-06): when this value changes while an error is
  // showing, the caught-error state resets — so a route-scoped boundary can
  // recover on navigation WITHOUT remounting (a key-based remount during
  // error recovery re-throws inside the recovery commit and escapes to the
  // outer boundary; a state reset in componentDidUpdate does not).
  resetOnChange?: string | number;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
  // Last-seen resetOnChange value; tracked in state so
  // getDerivedStateFromProps can clear a caught error in the SAME render
  // pass as the location change (a componentDidUpdate reset costs an extra
  // commit round-trip on the recovery path).
  resetOnChangeValue?: string | number;
}

export class ErrorBoundary extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false, error: null, resetOnChangeValue: props.resetOnChange };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  // Issue #779 (UI-R2-06): navigation away from a crashed route resets the
  // caught error DURING the render that carries the new location — the same
  // resetKeys pattern react-error-boundary uses. A key-based remount must
  // not be used here: remounting during error recovery re-throws inside
  // the recovery commit and escapes to the outer boundary.
  static getDerivedStateFromProps(
    props: ErrorBoundaryProps,
    state: ErrorBoundaryState,
  ): ErrorBoundaryState | null {
    if (props.resetOnChange === state.resetOnChangeValue) return null;
    if (state.hasError) {
      return { hasError: false, error: null, resetOnChangeValue: props.resetOnChange };
    }
    return { hasError: state.hasError, error: state.error, resetOnChangeValue: props.resetOnChange };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo): void {
    console.error('ErrorBoundary caught an error:', error, errorInfo);
  }

  private handleRetry = (): void => {
    this.setState({ hasError: false, error: null });
  };

  render(): React.ReactNode {
    if (this.state.hasError) {
      if (typeof this.props.fallback === "function") {
        return this.props.fallback(this.handleRetry);
      }
      if (this.props.fallback) {
        return this.props.fallback;
      }

      return (
        <div className="flex flex-col items-center justify-center min-h-[50vh] p-8">
          <div className="w-full max-w-md">
            <ErrorState
              title="Something went wrong"
              description="An unexpected error occurred. Please try again."
              action={{ label: "Try Again", onClick: this.handleRetry }}
            />
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
