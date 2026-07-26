// consumer_final.cpp — 双摄像头延迟 YOLOv8（固定 SHM 单帧版）新模型6路输出12类
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <opencv2/core.hpp>
#include <opencv2/imgproc.hpp>
#include <opencv2/dnn.hpp>
#include <onnxruntime_cxx_api.h>
#include "spacemit_ort_env.h"
#include <vector>
#include <algorithm>
#include <unordered_map>
#include <cmath>
using namespace std;
using namespace cv;

// SHM layout per pipe: 640*480*3/2 = 460800 bytes NV12
typedef struct { volatile int ready, size, w, h; unsigned char data[460800]; } fbuf_t;

// YOLOv8 — 12类: 轿车 货车 红灯 红灯左转灯 绿灯 黄灯 绿灯左转灯 三轮车 行人 摩托车 红灯右转灯 绿灯右转灯
static const vector<string> kL={"轿车","货车","红灯","红灯左转灯","绿灯","黄灯",
    "绿灯左转灯","三轮车","行人","摩托车","红灯右转灯","绿灯右转灯"};
static constexpr int IW=320,IH=320,NC=12;
static constexpr float CT=0.15f,NT=0.45f;
struct Det{Rect box;int cls;float sc;};
static inline void softmax(const float* l,int n,float* o){
    float m=l[0];for(int i=1;i<n;i++)if(l[i]>m)m=l[i];
    float s=0;for(int i=0;i<n;i++){o[i]=exp(l[i]-m);s+=o[i];}
    float inv=1.0f/s;for(int i=0;i<n;i++)o[i]*=inv;
}
static inline float sigmoidf(float x){return 1.0f/(1.0f+expf(-x));}

static vector<Det> postproc(const float* r0,const float* s0,const float* r1,const float* s1,const float* r2,const float* s2,int ow,int oh){
    struct S{const float* r;const float* s;int f,str;};
    S sc[3]={{r0,s0,40,8},{r1,s1,20,16},{r2,s2,10,32}};
    vector<Rect> bx;vector<float> ss;vector<int> ci;
    float sx=(float)ow/IW,sy=(float)oh/IH;
    for(int si=0;si<3;si++){int H=sc[si].f,W=sc[si].f,HW=H*W;
    for(int y=0;y<H;y++)for(int x=0;x<W;x++){int idx=y*W+x;float ms=0;int mc=-1;
    // Pre-sigmoid logits → sigmoid → max
    float ms_raw=-1e9;
    for(int c=0;c<NC;c++){float v=sc[si].s[c*HW+idx];if(v>ms_raw){ms_raw=v;mc=c;}}
    ms=sigmoidf(ms_raw);
    if(ms<CT)continue;float d[4],sm[16];
    for(int k=0;k<4;k++){float lg[16];for(int b=0;b<16;b++)lg[b]=sc[si].r[(k*16+b)*HW+idx];
    softmax(lg,16,sm);float ev=0;for(int b=0;b<16;b++)ev+=b*sm[b];d[k]=ev;}
    float ax=(x+0.5f)*sc[si].str,ay=(y+0.5f)*sc[si].str;
    float x1=ax-d[0]*sc[si].str,y1=ay-d[1]*sc[si].str;
    float x2=ax+d[2]*sc[si].str,y2=ay+d[3]*sc[si].str;
    int X1=(int)(x1*sx),Y1=(int)(y1*sy),X2=(int)(x2*sx),Y2=(int)(y2*sy);
    int BW=X2-X1,BH=Y2-Y1;X1=max(0,min(X1,ow-1));Y1=max(0,min(Y1,oh-1));
    BW=max(1,min(BW,ow-X1));BH=max(1,min(BH,oh-Y1));
    bx.emplace_back(X1,Y1,BW,BH);ss.push_back(ms);ci.push_back(mc);}}
    vector<int> kp;dnn::NMSBoxes(bx,ss,CT,NT,kp);
    vector<Det> rt;for(int i:kp)rt.push_back({bx[i],ci[i],ss[i]});return rt;
}

static volatile int g_stop=0;
static void onsig(int s){g_stop=1;}
static constexpr float DIRECT_SWITCH_THRESHOLD=0.75f;
static constexpr double DIRECT_SWITCH_MIN_INTERVAL=1.0;
static constexpr int FRAME_HEARTBEAT_INTERVAL=5;
static constexpr double ALERT_COOLDOWN=5.0;
static constexpr float ALERT_THRESHOLD=0.90f;
// Pipe0=CSI3左侧, Pipe1=CSI1右侧
// 不再内部播放WAV——改为打印[ALERT]到stdout，由Python语音助手管道读取后通过TW-TTS硬件合成语音

// 车辆类: 轿车(0), 货车(1), 三轮车(7), 摩托车(9)
static inline bool is_vehicle(int cls){return cls==0||cls==1||cls==7||cls==9;}
// 行人: (8)
static inline bool is_person(int cls){return cls==8;}

int main(int argc, char** argv){
    signal(SIGINT,onsig);signal(SIGTERM,onsig);
    const char* model=(argc>1)?argv[1]:"../best_6out.q.onnx";

    // ONNX
    Ort::Env env(ORT_LOGGING_LEVEL_WARNING,"CF");
    Ort::SessionOptions so;so.SetIntraOpNumThreads(4);
    so.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);
    unordered_map<string,string> eo;SessionOptionsSpaceMITEnvInit(so,eo);
    Ort::Session sess(env,model,so);
    const char* in[]={"images"};
    // 6路输出：reg0/reg1/reg2 + score0/score1/score2 (pre-sigmoid logits)
    const char* out[]={"/model.22/Reshape_output_0","/model.22/Reshape_1_output_0","/model.22/Reshape_2_output_0",
                       "/model.22/Reshape_3_output_0","/model.22/Reshape_4_output_0","/model.22/Reshape_5_output_0"};
    auto mi=Ort::MemoryInfo::CreateCpu(OrtArenaAllocator,OrtMemTypeDefault);

    // Attach both SHM buffers
    int fd0=shm_open("/pipe0_frame",O_RDWR,0);
    int fd1=shm_open("/pipe1_frame",O_RDWR,0);
    if(fd0<0||fd1<0){
        fprintf(stderr,"FATAL: shm_open pipe0/1\n");
        if(fd0>=0)close(fd0);if(fd1>=0)close(fd1);return 1;
    }
    struct stat st0{},st1{};
    if(fstat(fd0,&st0)<0||fstat(fd1,&st1)<0||
       st0.st_size<(off_t)sizeof(fbuf_t)||st1.st_size<(off_t)sizeof(fbuf_t)){
        fprintf(stderr,"FATAL: invalid shared-memory size\n");
        close(fd0);close(fd1);return 1;
    }
    fbuf_t* fb0=(fbuf_t*)mmap(NULL,sizeof(fbuf_t),PROT_READ|PROT_WRITE,MAP_SHARED,fd0,0);
    fbuf_t* fb1=(fbuf_t*)mmap(NULL,sizeof(fbuf_t),PROT_READ|PROT_WRITE,MAP_SHARED,fd1,0);
    close(fd0);close(fd1);
    if(fb0==MAP_FAILED||fb1==MAP_FAILED){
        fprintf(stderr,"FATAL: mmap\n");
        if(fb0!=MAP_FAILED)munmap(fb0,sizeof(fbuf_t));
        if(fb1!=MAP_FAILED)munmap(fb1,sizeof(fbuf_t));
        return 1;
    }
    fprintf(stderr,"[INIT] ready0=%d ready1=%d\n",
            __atomic_load_n(&fb0->ready,__ATOMIC_ACQUIRE),
            __atomic_load_n(&fb1->ready,__ATOMIC_ACQUIRE));

    int active=0, fc=0, dc=0, last_ready[2]={0,0};
    double last_alert[2]={0,0}, det_start=0;
    double last_switch_mono=-DIRECT_SWITCH_MIN_INTERVAL;
    struct timespec t0,t1;clock_gettime(CLOCK_MONOTONIC,&t0);

    while(!g_stop){
        fbuf_t* fb = active ? fb1 : fb0;
        int cur = __atomic_load_n(&fb->ready, __ATOMIC_ACQUIRE);
        if((cur&1)!=0||cur==last_ready[active]){usleep(2000);continue;}

        // Only the Y plane is consumed. Validate all producer-controlled sizes.
        int fw=fb->w, fh=fb->h;
        if(fw<=0||fh<=0||fw>640||fh>480||
           (size_t)fw*(size_t)fh>sizeof(fb->data)||
           fb->size<(int)((size_t)fw*(size_t)fh)){
            fprintf(stderr,"[WARN] invalid frame metadata w=%d h=%d size=%d\n",
                    fw,fh,fb->size);continue;
        }
        Mat shared_gray(fh, fw, CV_8UC1, (void*)(fb->data));
        Mat gray=shared_gray.clone();
        if(__atomic_load_n(&fb->ready,__ATOMIC_ACQUIRE)!=cur)continue;
        last_ready[active]=cur;
        fc++;
        Mat bgr; cvtColor(gray, bgr, COLOR_GRAY2BGR);

        // YOLO
        Mat blob=dnn::blobFromImage(bgr,1.0/255.0,Size(IW,IH),Scalar(),true,false,CV_32F);
        vector<int64_t> ish={1,3,IH,IW};
        Ort::Value it=Ort::Value::CreateTensor<float>(mi,blob.ptr<float>(),blob.total(),ish.data(),ish.size());
        auto ot=sess.Run(Ort::RunOptions{nullptr},in,&it,1,out,6);
        // 输出顺序: reg0 reg1 reg2 score0 score1 score2
        auto det=postproc(ot[0].GetTensorData<float>(),ot[3].GetTensorData<float>(),
                          ot[1].GetTensorData<float>(),ot[4].GetTensorData<float>(),
                          ot[2].GetTensorData<float>(),ot[5].GetTensorData<float>(),bgr.cols,bgr.rows);
        float max_conf=0.0f;
        for(const auto& d:det) max_conf=max(max_conf,d.sc);
        double now=(double)time(NULL);
        if(!det.empty()){
            dc++;
            if(det_start==0) det_start=now;

            // 检测车辆 → 方向告警（score >= 0.90 才触发）
            bool has_vehicle=false;
            for(auto& d:det) if(is_vehicle(d.cls) && d.sc>=ALERT_THRESHOLD){has_vehicle=true;break;}
            if(has_vehicle){
                const char* side=active?"右侧":"左侧";
                if(now-last_alert[active]>=ALERT_COOLDOWN){
                    last_alert[active]=now;
                    // 仅打印 [ALERT] 到 stdout，Python 语音助手通过管道读取后调用 TW-TTS 硬件合成语音
                    printf("[ALERT] %s有车辆靠近\n",side);
                }
            }
            printf("[F%d] P%d(%s) %zu:",fc,active,active?"右":"左",det.size());
            for(size_t i=0;i<det.size();i++)printf(" %s(%.2f)",kL[det[i].cls].c_str(),det[i].sc);
            printf("\n");fflush(stdout);

        }

        if(fc%FRAME_HEARTBEAT_INTERVAL==0){
            // 只供Python管理器判断帧进度，管理器不将其打印到用户日志。
            printf("[FRAME] %d\n",fc);fflush(stdout);
        }

        struct timespec switch_ts;clock_gettime(CLOCK_MONOTONIC,&switch_ts);
        double switch_now=switch_ts.tv_sec+switch_ts.tv_nsec*1e-9;

        // 当前画面最高目标置信度不足0.75（无目标时为0），但两次切换至少间隔1秒。
        if(max_conf<DIRECT_SWITCH_THRESHOLD &&
           switch_now-last_switch_mono>=DIRECT_SWITCH_MIN_INTERVAL){
            int prev=active; active=1-active; det_start=0;
            last_switch_mono=switch_now;
            fbuf_t* next=active?fb1:fb0;
            last_ready[active]=__atomic_load_n(&next->ready,__ATOMIC_ACQUIRE);
            printf("[SWITCH] P%d→P%d (max_conf %.2f < %.2f, min %.1fs)\n",
                   prev,active,max_conf,DIRECT_SWITCH_THRESHOLD,
                   DIRECT_SWITCH_MIN_INTERVAL); fflush(stdout);
        // 最高置信度达到0.75时，保留原有最多3秒强制切换。
        } else if(max_conf>=DIRECT_SWITCH_THRESHOLD && now-det_start>=3.0){
            int prev=active; active=1-active; det_start=0;
            last_switch_mono=switch_now;
            fbuf_t* next=active?fb1:fb0;
            last_ready[active]=__atomic_load_n(&next->ready,__ATOMIC_ACQUIRE);
            printf("[SWITCH] P%d→P%d (max 3s)\n",prev,active); fflush(stdout);
        }
        if(fc%30==0){
            clock_gettime(CLOCK_MONOTONIC,&t1);
            double dt=(t1.tv_sec-t0.tv_sec)+(t1.tv_nsec-t0.tv_nsec)*1e-9;
            printf("[STAT] FPS=%.1f P%d det=%d\n",30.0/dt,active,dc);fflush(stdout);
            t0=t1;dc=0;
        }
    }
    munmap(fb0,sizeof(fbuf_t));munmap(fb1,sizeof(fbuf_t));
    fprintf(stderr,"[DONE] frames=%d\n",fc);
    return 0;
}
