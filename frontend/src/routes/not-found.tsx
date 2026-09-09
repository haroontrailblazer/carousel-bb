import { Link } from "react-router"

import { BrandLogo } from "@/components/layout/brand-logo"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"

export function NotFoundRoute() {
  return (
    <div className="grid min-h-dvh place-items-center px-4">
      <Card className="max-w-sm p-8 text-center">
        <BrandLogo className="mx-auto mb-4 size-12" />
        <p className="text-3xl font-semibold tracking-tight">404</p>
        <p className="mt-2 text-sm text-[var(--muted-foreground)]">
          There is nothing at this address.
        </p>
        <Button variant="brand" className="mt-5" asChild>
          <Link to="/new" viewTransition>Go to the console</Link>
        </Button>
      </Card>
    </div>
  )
}
