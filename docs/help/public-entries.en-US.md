# Configure public access addresses

Team owners and administrators open **Team settings → Public access** to manage the browser-visible addresses Bowhead can use in generated links, invitations and callbacks.

1. Choose **Add entry**, and enter the scheme, hostname, optional port and deployed path prefix, for example the prefix /bowhead. Use the address people and external services actually reach. Leave out query parameters and fragments.
2. Open the one-time verification link in a browser that can reach that address. Confirm it leads back to this Bowhead instance. If the link expires, issue a new one. Recheck the list for an active entry.
3. Use **Make Default** for the address to use when background work needs to generate a team link. When replacing a default, verify the replacement before disabling the old entry. Check invitations and callbacks that are tied to the old address and update them where appropriate.

If verification fails, check the domain, proxy route and full prefix from the recipient’s network. Ordinary sign-in follows the address the visitor opened. Addresses registered here help with generated links and callbacks.
