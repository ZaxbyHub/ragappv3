import ReactMarkdown from "react-markdown";
import React from "react";
import remarkGfm from "remark-gfm";

const MARKDOWN_COMPONENTS = {
  code({ className, children, ...props }: any) {
    const isInline = !className;
    return isInline ? (
      <code className="bg-muted px-1 py-0.5 rounded-sm text-sm font-mono" {...props}>
        {children}
      </code>
    ) : (
      <pre className="bg-muted p-3 rounded-sm overflow-x-auto my-2">
        <code className="text-sm font-mono" {...props}>
          {children}
        </code>
      </pre>
    );
  },
  ul({ children }: any) {
    return <ul className="list-disc pl-5 my-2">{children}</ul>;
  },
  ol({ children }: any) {
    return <ol className="list-decimal pl-5 my-2">{children}</ol>;
  },
  li({ children }: any) {
    return <li className="my-0.5">{children}</li>;
  },
  p({ children }: any) {
    return <p className="my-2">{children}</p>;
  },
  h1({ children }: any) {
    return <h1 className="text-xl font-bold my-3">{children}</h1>;
  },
  h2({ children }: any) {
    return <h2 className="text-lg font-bold my-2">{children}</h2>;
  },
  h3({ children }: any) {
    return <h3 className="text-base font-bold my-2">{children}</h3>;
  },
  blockquote({ children }: any) {
    return <blockquote className="border-l-2 border-muted-foreground pl-3 italic my-2">{children}</blockquote>;
  },
  // Table components for GFM table support
  table({ children }: any) {
    return (
      <div className="overflow-x-auto my-4">
        <table className="min-w-full border-collapse border border-border text-sm">
          {children}
        </table>
      </div>
    );
  },
  thead({ children }: any) {
    return <thead className="bg-muted">{children}</thead>;
  },
  tbody({ children }: any) {
    return <tbody>{children}</tbody>;
  },
  tr({ children }: any) {
    return <tr className="border-b border-border">{children}</tr>;
  },
  th({ children }: any) {
    return (
      <th className="border border-border px-3 py-2 text-left font-semibold">
        {children}
      </th>
    );
  },
  td({ children }: any) {
    return (
      <td className="border border-border px-3 py-2">
        {children}
      </td>
    );
  },
};

export const MarkdownContent = React.memo(function MarkdownContent({ content }: { content: string }) {
  return (
    <div className="prose prose-sm dark:prose-invert max-w-prose">
      <ReactMarkdown
        components={MARKDOWN_COMPONENTS}
        remarkPlugins={[remarkGfm]}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});
