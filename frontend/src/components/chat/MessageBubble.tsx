import { memo } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { AlertCircle } from "lucide-react";
import { cn } from "@/lib/utils";
import { MarkdownMessage } from "./MarkdownMessage";
import { UserMessageActions } from "./MessageActions";
import type { Message } from "@/stores/useChatStore";
import { formatRelativeTime } from "@/lib/formatters";

interface MessageBubbleProps {
  message: Message;
  isStreaming?: boolean;
  isEditDisabled?: boolean;
  onFork?: () => void;
  userInitial: string;
  onEdit?: (messageId: string, content: string) => void;
}

export const MessageBubble = memo(function MessageBubble({
  message,
  isStreaming,
  isEditDisabled = false,
  onFork,
  userInitial,
  onEdit,
}: MessageBubbleProps) {
  const isUser = message.role === "user";
  const prefersReducedMotion = useReducedMotion();

  return (
    <motion.div
      initial={prefersReducedMotion ? { opacity: 0 } : { opacity: 0, y: 8 }}
      animate={prefersReducedMotion ? { opacity: 1 } : { opacity: 1, y: 0 }}
      transition={{ duration: prefersReducedMotion ? 0.1 : 0.25 }}
      className={cn(
        "group flex gap-3 px-4 py-5",
        isUser && "justify-end"
      )}
      role="article"
      aria-label={isUser ? "Your message" : "Assistant message"}
    >
      {/* User: avatar on right side — rendered after content via flex-row-reverse */}
      {isUser ? (
        <>
          {/* Content bubble */}
          <div className="flex flex-col items-end min-w-0 max-w-[68ch]">
            {/* Name + timestamp row */}
            <div className="flex items-center gap-2 mb-2">
              {message.created_at && (
                <time
                  className="text-[10px] text-muted-foreground/50 opacity-0 group-hover:opacity-100 [@media(pointer:coarse)]:opacity-100 transition-opacity"
                  dateTime={message.created_at}
                  title={new Date(message.created_at).toLocaleString()}
                >
                  {formatRelativeTime(message.created_at)}
                </time>
              )}
              <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">You</span>
            </div>

            {/* Message bubble */}
            <div
              className={cn(
                "rounded-tl-2xl rounded-bl-2xl rounded-br-2xl rounded-tr px-4 py-3 text-sm leading-relaxed",
                "bg-primary text-primary-foreground",
                "max-w-full break-words"
              )}
            >
              {message.content}
            </div>

            {/* Error */}
            {message.error && (
              <div className="mt-2 flex items-start gap-2 rounded-sm bg-destructive/10 border border-destructive/20 p-3 text-left">
                <AlertCircle className="h-4 w-4 text-destructive flex-shrink-0 mt-0.5" aria-hidden />
                <div>
                  <p className="text-sm font-medium text-destructive">Error</p>
                  <p className="text-xs text-destructive/80 mt-0.5">{message.error}</p>
                </div>
              </div>
            )}

            {/* Actions */}
            <UserMessageActions
              content={message.content}
              onEdit={
                onEdit
                  ? () => {
                      if (!isEditDisabled) onEdit(message.id, message.content);
                    }
                  : undefined
              }
              isEditDisabled={isEditDisabled}
              onFork={onFork}
            />
          </div>

          {/* Avatar */}
          <div
            className="flex-shrink-0 w-7 h-7 mt-0.5 rounded-full flex items-center justify-center bg-primary text-primary-foreground self-start"
            aria-hidden
          >
            <span className="text-[10px] font-bold leading-none">{userInitial}</span>
          </div>
        </>
      ) : (
        // Fallback: assistant rendered via MessageBubble (shouldn't normally happen)
        <>
          <div className="flex-shrink-0 w-7 h-7 mt-0.5 rounded-full flex items-center justify-center bg-primary/10 text-primary" aria-hidden>
            <span className="text-xs font-semibold">{userInitial}</span>
          </div>
          <div className="flex-1 min-w-0 max-w-[68ch]">
            <MarkdownMessage
              content={message.content}
              isStreaming={isStreaming && !isUser}
            />
          </div>
        </>
      )}
    </motion.div>
  );
});
