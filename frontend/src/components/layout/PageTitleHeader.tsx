interface PageTitleHeaderProps {
  title: string;
  description?: string;
}

export function PageTitleHeader({ title, description }: PageTitleHeaderProps) {
  return (
    <div className="flex flex-col items-start justify-start gap-1 py-1.5 px-6 bg-accent/50 rounded-sm">
      <h1 className="text-3xl font-semibold tracking-tight">{title}</h1>
      {description && <p className="text-muted-foreground mt-1 font-normal">{description}</p>}
    </div>
  );
}
