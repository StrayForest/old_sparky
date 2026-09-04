export const HERO_CDN_BASE = "https://cdn.old-sparky.com/draft/heroes/v1";

const entries = [
  ["abrams", "Abrams"],
  ["apollo", "Apollo"],
  ["bebop", "Bebop"],
  ["billy", "Billy"],
  ["calico", "Calico"],
  ["celeste", "Celeste"],
  ["drifter", "Drifter"],
  ["dynamo", "Dynamo"],
  ["graves", "Graves"],
  ["grey-talon", "Grey Talon"],
  ["haze", "Haze"],
  ["holliday", "Holliday"],
  ["infernus", "Infernus"],
  ["ivy", "Ivy"],
  ["kelvin", "Kelvin"],
  ["lady-geist", "Lady Geist"],
  ["lash", "Lash"],
  ["mcginnis", "McGinnis"],
  ["mina", "Mina"],
  ["mirage", "Mirage"],
  ["mo-and-krill", "Mo & Krill"],
  ["paige", "Paige"],
  ["paradox", "Paradox"],
  ["pocket", "Pocket"],
  ["rem", "Rem"],
  ["seven", "Seven"],
  ["shiv", "Shiv"],
  ["silver", "Silver"],
  ["sinclair", "Sinclair"],
  ["the-doorman", "The Doorman"],
  ["venator", "Venator"],
  ["victor", "Victor"],
  ["vindicta", "Vindicta"],
  ["viscous", "Viscous"],
  ["vyper", "Vyper"],
  ["warden", "Warden"],
  ["wraith", "Wraith"],
  ["yamato", "Yamato"]
];

export const HEROES = entries.map(([id, name]) => ({
  id,
  name,
  image: `${HERO_CDN_BASE}/${id}.webp`
}));

export const HERO_BY_ID = new Map(HEROES.map((hero) => [hero.id, hero]));
