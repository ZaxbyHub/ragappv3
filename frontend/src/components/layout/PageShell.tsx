import { ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { Navigation } from "./Navigation";
import { UploadIndicator } from "@/components/shared/UploadIndicator";
import type { HealthStatus } from "@/types/health";
import type { NavItemId } from "./navigationTypes";

interface PageShellProps {
  children: ReactNode;
  activeItem: NavItemId;
  onItemSelect: (id: NavItemId) => void;
  healthStatus: HealthStatus;
}

export function PageShell({ children, activeItem, onItemSelect, healthStatus }: PageShellProps) {
  const location = useLocation();
  const prefersReducedMotion = useReducedMotion();

  // Chat pages get edge-to-edge layout (no padding)
  const isChat = location.pathname.startsWith("/chat");

  // Suppress translate when user prefers reduced motion; keep a brief opacity
  // cross-fade so the page transition still has perceptible feedback.
  const pageVariants = prefersReducedMotion
    ? {
        initial: { opacity: 0 },
        animate: { opacity: 1 },
        exit: { opacity: 0 },
      }
    : {
        initial: { opacity: 0, y: 8 },
        animate: { opacity: 1, y: 0 },
        exit: { opacity: 0, y: -8 },
      };

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Skip navigation link (CR-3) */}
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:fixed focus:top-4 focus:left-4 focus:z-50 focus:px-4 focus:py-2 focus:bg-primary focus:text-primary-foreground focus:rounded-sm focus:text-sm focus:font-medium focus:shadow-lg"
      >
        Skip to main content
      </a>

      {/* Navigation - Responsive (Desktop Rail / Mobile Bottom Nav) */}
      <Navigation activeItem={activeItem} onItemSelect={onItemSelect} healthStatus={healthStatus} />

      {/* Main Content Area */}
      <main id="main-content" className="flex-1 flex flex-col min-h-0 min-w-0 overflow-hidden">
        <div className={isChat ? "flex-1 min-h-0 overflow-hidden" : "flex-1 min-h-0 p-6 lg:p-8 overflow-auto pb-20 md:pb-6 mx-auto w-full"}>
          <AnimatePresence mode="wait">
            <motion.div
              key={location.pathname}
              variants={pageVariants}
              initial="initial"
              animate="animate"
              exit="exit"
              transition={{ duration: prefersReducedMotion ? 0.1 : 0.15, ease: "easeOut" }}
              className="h-full"
            >
              {children}
            </motion.div>
          </AnimatePresence>
        </div>
      </main>

      {/* Global Upload Indicator - Shows on all pages */}
      <UploadIndicator />
    </div>
  );
}
