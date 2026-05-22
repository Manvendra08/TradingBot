import os

file_path = os.path.join('chrome_extension', 'content.js')

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

old_mp = '''let computedMaxPain = null;
  if (bestStrikes && bestStrikes.length > 0) {
      let validStrikes = bestStrikes.filter(s => s && s.strike).map(s => parseFloat(s.strike)).sort((a,b) => a-b);
      if(validStrikes.length > 0) {
         computedMaxPain = validStrikes[Math.floor(validStrikes.length / 2)];   
      }
  }'''

new_mp = '''let computedMaxPain = null;
  if (bestStrikes && bestStrikes.length > 0) {
      let strikes = [...new Set(bestStrikes.map(s => parseFloat(s.strike)))].filter(s => !isNaN(s)).sort((a,b) => a-b);
      if (strikes.length > 0) {
          let minLoss = Infinity;
          for (const testStrike of strikes) {
              let loss = 0;
              for (const opt of bestStrikes) {
                  const s = parseFloat(opt.strike);
                  const oi = opt.oi || 0;
                  if (opt.option_type === 'CE' && s < testStrike) {
                      loss += oi * (testStrike - s);
                  } else if (opt.option_type === 'PE' && s > testStrike) {
                      loss += oi * (s - testStrike);
                  }
              }
              if (loss < minLoss) {
                  minLoss = loss;
                  computedMaxPain = testStrike;
              }
          }
      }
  }'''

content = content.replace(old_mp, new_mp)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)
print("done")
