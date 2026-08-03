/**
 * Settings → Maintenance tab → Draft Room.
 *
 * Draft Room is an admin opt-in feature (SPEC section 15, ``draft_room_enabled``):
 * enabling it turns on the Draft Room workspace and, per SPEC section 16.2, its
 * navigation entry — nav renders only when capabilities reports enabled=true.
 * The toggle lives in Settings rather than being on by default.
 */
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import type { SettingsFormData } from "@/stores/useSettingsStore";

export interface DraftRoomSettingsProps {
  formData: SettingsFormData;
  onChange: <K extends keyof SettingsFormData>(
    field: K,
    value: SettingsFormData[K],
  ) => void;
}

export function DraftRoomSettings({ formData, onChange }: DraftRoomSettingsProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Draft Room</CardTitle>
        <CardDescription>
          Draft Room is an admin opt-in feature. Enabling it turns on the
          Draft Room workspace and its navigation entry for all users.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-center gap-2">
          <Checkbox
            id="draft-room-enabled"
            checked={formData.draft_room_enabled}
            onCheckedChange={(v) => onChange("draft_room_enabled", Boolean(v))}
          />
          <Label htmlFor="draft-room-enabled" className="text-sm font-normal">
            Enable Draft Room
          </Label>
        </div>
      </CardContent>
    </Card>
  );
}
