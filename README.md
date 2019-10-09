# DKB VISA and Giro Exporter with 2FA

Based on https://github.com/hoffie/dkb-visa. 
Changes: 

* removed QIF export
* added export for giro accounts
* compatibility with Python 3
* export of all available data to csv, not just a specified card
* output directory instead of output file
* make it work with confirmation through banking app instead of TAN

## How does it work?

It will log-in as you at DKB's online banking website, will pretend to be
you and will use the CSV export feature.

## Usage

```shell
./dkb.py --userid USER
```

with USER being the name you are using at the regular DKB online banking web site. 
You will be asked for a PIN and possibly a TAN.

Use `./dkb.py --help` for a more comprehensible guide on the available options. 
