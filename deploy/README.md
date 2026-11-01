# Putting Dental OS on the clinic's own computer

This is for a clinic that runs Dental OS itself, on a small computer at the clinic, so daily work carries on when the
internet is down. A clinic on the hosted service does not need any of this.

## What to buy

A mini PC with an SSD and 8 GB of memory is plenty for one clinic, with a UPS so a power cut does not stop it mid-write.
Put Ubuntu Server 24.04 LTS on it and plug it into the clinic's router with a cable, not Wi-Fi. Give it a fixed address in
the router so the name below always points at it. Buy a USB disk for the off-site copy, or use a cloud folder that speaks
WebDAV, and keep the disk somewhere other than the server room.

## Install

From the unpacked Dental OS folder on that computer:

```
sudo sh deploy/install-ubuntu.sh
```

It installs Python and Caddy from Ubuntu's own packages, puts the code in `/opt/dental-os`, keeps the data in
`/var/lib/dental-os`, writes the settings file `/etc/dental-os.env` with a new random `SECRET_KEY`, starts Dental OS as a
service, and adds the scheduled jobs (`/etc/cron.d/dental-os`). Running it again later updates the code and leaves the data
and settings alone.

Then:

1. Edit `/etc/dental-os.env`: `PUBLIC_URL` (for example `https://dental.local`), `OFFSITE_TARGET` and `OFFSITE_PASSPHRASE`.
   Write the passphrase on paper and keep it away from the computer: without it the copies cannot be opened, by anyone.
   `sudo systemctl restart dental-os` afterwards.
2. In the router, point the name `dental.local` at this computer (or change the name in `/etc/caddy/Caddyfile` to the one you
   use). `sudo systemctl reload caddy` after any change there.
3. Caddy makes its own certificate for the clinic network. Each computer and tablet that uses Dental OS has to trust Caddy's
   local root certificate once, or the browser will warn every time. On the Ubuntu package it is usually under
   `/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`; copy it to each device and add it as a trusted root.
4. `sudo -u dentalos /opt/dental-os/deploy/dos create-clinic`, then `create-user` for the first manager.
5. `sudo -u dentalos /opt/dental-os/deploy/dos doctor` lists what is still to do. The same list is in the app under
   Settings, Ready to go live.

## Each day and each month

The scheduled jobs make a local backup every night, an off-site copy after it, and a restore drill on the 1st of the month.
Settings, Copies shows whether they worked. Take the USB disk home at least once a week if that is where the copies go.

## The first weeks

Keep the old system running. Each evening the manager types the old system's takings, appointments and new patients for
the day under Settings, Ready to go live, Side by side. Dental OS shows its own figures next to them. When they have
matched for ten working days in a row, and Settings, Ready to go live shows nothing left to do, the old system can go.

Staff report anything that goes wrong with the "Report a problem" link at the bottom of every page. If a page breaks,
it shows a reference like `E-7K2Q9X`; the same reference is in the problem list and in the server log
(`journalctl -u dental-os`) with the full details.
