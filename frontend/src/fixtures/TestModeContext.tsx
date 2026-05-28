import { createContext, useContext } from "react";

const TestModeContext = createContext<boolean>(false);

export function TestModeProvider({
  testMode,
  children,
}: {
  testMode: boolean;
  children: React.ReactNode;
}) {
  return (
    <TestModeContext.Provider value={testMode}>
      {children}
    </TestModeContext.Provider>
  );
}

export function useTestMode(): boolean {
  return useContext(TestModeContext);
}
