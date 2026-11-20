# Brief: internal link shortener

Build a URL shortener service for our support team.

- Users create a short link from a long http(s) URL and may choose a custom alias of 4 to 32 characters.
- Visiting a short link redirects to the original URL in under 50 ms at the 95th percentile.
- Links can optionally expire after a given number of days.
- The service must handle 500 redirects per second and 20 link creations per second.
- Admins can see click counts per link for the last 30 days.
- Admins must authenticate with an API key; redirects are public.
- Deploy with Docker on a single host. Use PostgreSQL for storage.
