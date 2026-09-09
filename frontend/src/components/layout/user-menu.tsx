import * as React from "react"
import { LogOut, User } from "lucide-react"
import { Link } from "react-router"

import { UserAvatar } from "@/components/layout/user-avatar"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { useAuth } from "@/hooks/use-auth"
import { useProfile } from "@/hooks/use-profile"
import { prefetchRouteChunk } from "@/lib/route-chunks"

/**
 * The account control at the foot of the sidebar.
 *
 * A menu rather than a link, so the two things people want from an account
 * row - "who am I signed in as" and "get me out" - are one click away, and
 * everything else lives behind Profile.
 */
export function UserMenu({ onNavigate }: { onNavigate?: () => void }) {
  const { identity, signOut } = useAuth()
  const { profile } = useProfile()
  const [open, setOpen] = React.useState(false)
  const [signingOut, setSigningOut] = React.useState(false)

  return (
    <DropdownMenu
      open={open}
      // Stay open while signing out, so the label change is visible instead of
      // the menu snapping shut with no feedback.
      onOpenChange={(next) => !signingOut && setOpen(next)}
    >
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="flex w-full items-center gap-2 rounded-[var(--radius-md)] px-2.5 py-2 text-left transition-colors hover:bg-[var(--muted)]"
        >
          <UserAvatar
            key={profile.avatarUrl ?? "none"}
            src={profile.avatarUrl}
            name={profile.displayName}
            seed={profile.email}
            className="size-7 text-[11px]"
          />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-xs font-medium">
              {profile.displayName}
            </span>
            <span className="block truncate text-[11px] text-[var(--muted-foreground)]">
              {identity?.role ?? ""}
            </span>
          </span>
        </button>
      </DropdownMenuTrigger>

      <DropdownMenuContent side="top" align="start" className="w-[15rem]">
        <DropdownMenuLabel>
          <span className="flex items-center gap-2.5">
            <UserAvatar
              key={profile.avatarUrl ?? "none"}
              src={profile.avatarUrl}
              name={profile.displayName}
              seed={profile.email}
              className="size-8 text-xs"
            />
            <span className="min-w-0">
              <span className="block truncate text-sm font-medium">
                {profile.displayName}
              </span>
              <span className="block truncate text-xs text-[var(--muted-foreground)]">
                {profile.email}
              </span>
            </span>
          </span>
        </DropdownMenuLabel>

        <DropdownMenuSeparator />

        <DropdownMenuItem asChild>
          <Link
            to="/profile"
            viewTransition
            // Same trick as the sidebar: the screen is downloading while the
            // menu is still open, so the click lands on something ready.
            onPointerEnter={() => prefetchRouteChunk("/profile")}
            onFocus={() => prefetchRouteChunk("/profile")}
            onClick={() => onNavigate?.()}
          >
            <User className="size-4" /> Profile and settings
          </Link>
        </DropdownMenuItem>

        <DropdownMenuItem
          destructive
          disabled={signingOut}
          // preventDefault keeps the menu open so the pending label shows.
          onSelect={(event) => {
            event.preventDefault()
            setSigningOut(true)
            void signOut()
          }}
        >
          <LogOut className="size-4" />
          {signingOut ? "Signing out…" : "Sign out"}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
