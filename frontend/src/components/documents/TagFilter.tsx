import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { Tag } from "@/lib/api";

const ALL_VALUE = "__all__";

interface TagFilterProps {
  tags: Tag[];
  value: number | null;
  onChange: (tagId: number | null) => void;
}

export function TagFilter({ tags, value, onChange }: TagFilterProps) {
  if (tags.length === 0) return null;
  return (
    <Select
      value={value == null ? ALL_VALUE : String(value)}
      onValueChange={(v) => onChange(v === ALL_VALUE ? null : Number(v))}
    >
      <SelectTrigger className="w-44" aria-label="Filter by tag">
        <SelectValue placeholder="Filter by tag" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={ALL_VALUE}>All tags</SelectItem>
        {tags.map((tag) => (
          <SelectItem key={tag.id} value={String(tag.id)}>
            {tag.name} ({tag.document_count})
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
