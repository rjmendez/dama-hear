// Path test: live PDM mic + a bench PPS, end to end, with no GPS and no radio.
//
// Wire a jumper from D0 (GPIO1) to D1 (GPIO2). D0 emits a 1 Hz square wave standing in for GPS
// PPS; D1 captures the edge. That is the friend's shared-PPS bench trick from
// acoustic-triangulation/firmware/src/twonode_selftest.cpp, which is the single largest reason a
// timing claim can be tested on a desk instead of in a field.
//
// ⚠️WHAT A SELF-GENERATED PPS CANNOT TEST. The pulse and esp_timer come off the SAME crystal, so
// the measured interval is that oscillator against itself and says NOTHING about clock discipline.
// Real discipline needs the GPS. What this DOES measure honestly:
//   * that the capture path fires at all, and its jitter (the cost of an ISR vs hardware capture)
//   * the TRUE I2S sample rate against the CPU clock -- the 48000-vs-47619 class of trap, which
//     no amount of datasheet reading gives you
//   * the whole DSP chain on real audio: gate -> sketch -> 172 B frame
#include <ESP_I2S.h>
#include "driver/gpio.h"
#include "mel16.h"
#include "golden.h"          // window, and the fft/sketch/pack implementation's constants

#define PPS_OUT 1            // D0
#define PPS_IN  2            // D1
#define PDM_CLK 42
#define PDM_DIN 41

#define RUN_S       20
#define PPS_HZ      10       // bench pulse rate; every number below comes from MEASURED
#define PPS_RES     14       // intervals, so the rate is arbitrary.
//
// 1 Hz is not reachable here and it took three tries to stop guessing why. LEDC freq =
// clk / (div * 2^res). The divider is ~10 bits (max 1023.99) and the ESP32-S3 LEDC timer is
// 14 bits wide -- the 20-bit range belongs to the original ESP32. So on the 80 MHz APB the floor
// is 80e6/(1024*2^14) = 4.8 Hz. Low frequency needs HIGH resolution, and this part runs out of
// resolution before it reaches 1 Hz. 10 Hz at 14-bit needs divider 488: comfortable.
#define BLOCK       512

// ---- ISR-captured PPS edges -------------------------------------------------
#define MAXE 512
static volatile uint32_t e_us[MAXE];
static volatile uint32_t e_blk[MAXE];
static volatile int      e_n = 0;
static volatile uint32_t blocks_done = 0;

// Bench pulse generator. LEDC was the obvious tool and cost four attempts: 1 Hz needs a divider
// its ~10-bit field cannot hold, the S3's LEDC timer is 14-bit not 20-bit so raising resolution
// runs out before 1 Hz, and even a reachable 10 Hz/14-bit config reported attach-ok while driving
// nothing. A timer ISR toggling the pad is less elegant and actually works. Its own jitter lands
// in the measured spread, so the spread below is generator + capture, not capture alone.
static hw_timer_t *pps_timer = NULL;
static volatile bool pps_level = false;
static void IRAM_ATTR pps_gen_isr() { pps_level = !pps_level; digitalWrite(PPS_OUT, pps_level); }

static void IRAM_ATTR pps_isr() {
  if (e_n < MAXE) {
    e_us[e_n]  = (uint32_t)esp_timer_get_time();
    e_blk[e_n] = blocks_done;
    e_n++;
  }
}

// ---- DSP (same code path as hear_poc, retargeted to 16 kHz) -----------------
static float fft_re[GOLD_NFFT], fft_im[GOLD_NFFT];
static float tw_re[GOLD_NFFT/2], tw_im[GOLD_NFFT/2];
static void fft_init(){ for(int k=0;k<GOLD_NFFT/2;k++){ float a=-2.0f*(float)M_PI*k/GOLD_NFFT;
  tw_re[k]=cosf(a); tw_im[k]=sinf(a);} }
static void fft256(){
  const int N=GOLD_NFFT;
  for(int i=1,j=0;i<N;i++){ int bit=N>>1; for(;j&bit;bit>>=1) j^=bit; j^=bit;
    if(i<j){ float t=fft_re[i];fft_re[i]=fft_re[j];fft_re[j]=t; t=fft_im[i];fft_im[i]=fft_im[j];fft_im[j]=t; } }
  for(int len=2;len<=N;len<<=1){ int step=N/len;
    for(int i=0;i<N;i+=len) for(int k=0;k<len/2;k++){
      int a=i+k,b=a+len/2; float cr=tw_re[k*step],ci=tw_im[k*step];
      float xr=fft_re[b]*cr-fft_im[b]*ci, xi=fft_re[b]*ci+fft_im[b]*cr;
      fft_re[b]=fft_re[a]-xr; fft_im[b]=fft_im[a]-xi; fft_re[a]+=xr; fft_im[a]+=xi; } }
}
static void sketch16(const int16_t*x,int n,int8_t*q,float*ref){
  static float db[GOLD_BANDS*GOLD_FRAMES];
  for(int t=0;t<GOLD_FRAMES;t++){
    int s=t*MEL16_HOP;
    for(int i=0;i<GOLD_NFFT;i++){ int k=s+i; fft_re[i]=((k<n)?(float)x[k]:0.0f)*GOLD_WIN[i]; fft_im[i]=0.0f; }
    fft256();
    const float*w=MEL16_FB_W;
    for(int b=0;b<GOLD_BANDS;b++){ int lo=MEL16_FB_LO[b],cnt=MEL16_FB_N[b]; float acc=0;
      for(int i=0;i<cnt;i++){ int bin=lo+i; acc+=w[i]*(fft_re[bin]*fft_re[bin]+fft_im[bin]*fft_im[bin]); }
      w+=cnt; db[b*GOLD_FRAMES+t]=10.0f*log10f(acc+1e-12f); } }
  float r=db[0]; for(int i=1;i<GOLD_BANDS*GOLD_FRAMES;i++) if(db[i]>r) r=db[i];
  *ref=r;
  for(int i=0;i<GOLD_BANDS*GOLD_FRAMES;i++){ float v=roundf((db[i]-r)*2.0f);
    q[i]=(int8_t)(v<-128?-128:(v>127?127:v)); }
}
static int pack172(uint8_t*o,uint32_t us,float ref,uint16_t pk,const int8_t*q,uint16_t fl){
  int16_t r4=(int16_t)lrintf(ref*4.0f);
  o[0]=us; o[1]=us>>8; o[2]=us>>16; o[3]=us>>24; o[4]=r4; o[5]=r4>>8;
  o[6]=pk; o[7]=pk>>8; o[8]=GOLD_BANDS; o[9]=GOLD_FRAMES; o[10]=fl; o[11]=fl>>8;
  memcpy(o+12,q,GOLD_BANDS*GOLD_FRAMES); return 12+GOLD_BANDS*GOLD_FRAMES;
}

// ---- causal gate ------------------------------------------------------------
static float g_amb=0, g_env_sum=0, g_env_buf[16]; static int g_env_i=0, g_armed=1;
static const int   G_EN=16;                       // 1 ms at 16 kHz
static const float G_INV=1.0f/16.0f, G_ALPHA=1.0f/10000.0f, G_RATIO=8.0f, G_FLOOR=800.0f, G_REARM=0.35f;
static int gate(int16_t s,float*env_out){
  float a=fabsf((float)s);
  g_env_sum += a-g_env_buf[g_env_i]; g_env_buf[g_env_i]=a;
  if(++g_env_i>=G_EN) g_env_i=0;
  float e=g_env_sum*G_INV; *env_out=e;
  float thr=g_amb*G_RATIO; if(thr<G_FLOOR) thr=G_FLOOR;
  if(!g_armed){ if(e<thr*G_REARM) g_armed=1; return 0; }
  if(e<=thr){ g_amb=(1.0f-G_ALPHA)*g_amb+G_ALPHA*e; return 0; }
  g_armed=0; return 1;
}

static I2SClass i2s;
static int16_t blk[BLOCK];
static int16_t ring[GOLD_NFFT + 8*64 + 64];

void setup(){
  Serial.begin(115200); delay(2500);
  Serial.println("\n=== dama-hear path test : live PDM + bench PPS ===");
  fft_init();

  pinMode(PPS_IN, INPUT_PULLDOWN);
  attachInterrupt(digitalPinToInterrupt(PPS_IN), pps_isr, RISING);

  pinMode(PPS_OUT, OUTPUT);
  digitalWrite(PPS_OUT, LOW);
  gpio_set_direction((gpio_num_t)PPS_OUT, GPIO_MODE_INPUT_OUTPUT);   // so the pad can be read back
  pps_timer = timerBegin(1000000);                                    // 1 MHz ticks
  timerAttachInterrupt(pps_timer, &pps_gen_isr);
  timerAlarm(pps_timer, 500000 / PPS_HZ, true, 0);                    // half-period -> PPS_HZ square
  bool gen = (pps_timer != NULL);

  // Prove the generator independently of the jumper, so a dead pulse and a missing wire cannot
  // be confused for each other -- which is exactly what happened the first time this ran.
  int transitions = 0, last = digitalRead(PPS_OUT);
  uint32_t tw = millis();
  while (millis() - tw < 400) { int v = digitalRead(PPS_OUT); if (v != last) { transitions++; last = v; } }
  Serial.printf("pps gen  attach %s, %d transitions in 400 ms on D0 -> %s\n",
                gen ? "ok" : "FAILED", transitions,
                transitions > 2 ? "pulse is live" : "NO PULSE");
  Serial.printf("pps cap  D0 (GPIO1) --jumper--> D1 (GPIO2), %d Hz\n", PPS_HZ);

  i2s.setPinsPdmRx(PDM_CLK, PDM_DIN);
  if(!i2s.begin(I2S_MODE_PDM_RX, (int)MEL16_FS, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)){
    Serial.println("i2s begin FAILED"); return; }
  Serial.printf("mic: PDM %d Hz nominal on clk=%d din=%d\n\n", (int)MEL16_FS, PDM_CLK, PDM_DIN);
  Serial.printf("capturing %d s -- make a loud noise (clap) to fire the gate\n\n", RUN_S);

  uint32_t t_start = (uint32_t)esp_timer_get_time();
  uint32_t nsamp=0, dets=0; int rpos=0; float envmax=0, envsum=0; uint32_t envn=0;
  int8_t q[GOLD_BANDS*GOLD_FRAMES]; uint8_t frame[200];
  int pending=-1;

  while((uint32_t)esp_timer_get_time()-t_start < (uint32_t)RUN_S*1000000UL){
    size_t got=i2s.readBytes((char*)blk,sizeof blk);
    int n=got/2; if(n<=0) continue;
    for(int i=0;i<n;i++){
      int16_t s=blk[i];
      ring[rpos]=s; rpos=(rpos+1)%(int)(sizeof(ring)/2);
      float e; 
      if(gate(s,&e)){
        dets++;
        if(pending<0) pending=1;
        // build a frame from the ring starting here; ring order is approximate at the wrap,
        // which is fine for a path test and NOT fine for a measurement.
        float ref; sketch16(ring, sizeof(ring)/2, q, &ref);
        uint32_t node_us = (uint32_t)(esp_timer_get_time() % 1000000);
        int len=pack172(frame,node_us,ref,(uint16_t)min((int)fabsf((float)s),65535),q,0);
        if(dets<=3){
          Serial.printf("  detection %lu  node_us %lu  ref_db %.1f  frame %d B: ",
                        (unsigned long)dets,(unsigned long)node_us,ref,len);
          for(int k=0;k<12;k++) Serial.printf("%02x",frame[k]);
          Serial.printf("... (+%d sketch bytes)\n", len-12);
        }
      }
      envsum+=e; envn++; if(e>envmax) envmax=e;
      nsamp++;
    }
    blocks_done++;
  }

  Serial.printf("\nmic     %lu samples in %d s\n",(unsigned long)nsamp,RUN_S);
  Serial.printf("        envelope mean %.0f  max %.0f  gate fired %lu time(s)\n",
                envn?envsum/envn:0.0f, envmax, (unsigned long)dets);

  int ne=e_n;
  if(ne<3){ Serial.printf("\npps     only %d edge(s) captured. Generator state is printed above:\n"
                          "        pulse live + no edges = the D0->D1 jumper is missing.\n"
                          "        no pulse = generator problem, not wiring.\n", ne); }
  else{
    double isum=0,imin=1e12,imax=0;
    for(int i=1;i<ne;i++){ double d=(double)(e_us[i]-e_us[i-1]); isum+=d; if(d<imin)imin=d; if(d>imax)imax=d; }
    double imean=isum/(ne-1);
    double blocks=(double)(e_blk[ne-1]-e_blk[0]);
    double secs=(double)(e_us[ne-1]-e_us[0])/1e6;
    double rate=blocks*BLOCK/secs;
    Serial.printf("\npps     %d edges  interval mean %.1f us (nominal %d)  spread %.1f us (min %.0f max %.0f)\n",
                  ne,imean,1000000/PPS_HZ,imax-imin,imin,imax);
    Serial.printf("        ^ same crystal drives pulse and timer, so this is ISR jitter only,\n");
    Serial.printf("          NOT clock discipline. Discipline needs the GPS.\n");
    Serial.printf("rate    I2S measured %.1f Hz vs %.0f nominal  (%+.0f ppm)\n",
                  rate, MEL16_FS, (rate/MEL16_FS-1.0)*1e6);
    Serial.printf("        block granularity %d samples = %.1f ms, so this resolves ~%.0f ppm\n",
                  BLOCK, 1000.0*BLOCK/MEL16_FS, 1e6*BLOCK/(MEL16_FS*secs));
  }
  Serial.println("\n=== done ===");
}
void loop(){ delay(1000); }
