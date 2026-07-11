import { Link } from "react-router-dom";
import { FileQuestion } from "lucide-react";
import { EmptyState } from "@/components/EmptyState";
import { Button } from "@/components/ui/button";

export default function NotFoundPage() {
  return (
    <div className="flex flex-col items-center justify-center min-h-screen p-8">
      <div className="w-full max-w-md">
        <EmptyState
          icon={FileQuestion}
          size="lg"
          title="404"
          description="This page doesn't exist."
          action={
            <Button asChild>
              <Link to="/">Go Home</Link>
            </Button>
          }
        />
      </div>
    </div>
  );
}
